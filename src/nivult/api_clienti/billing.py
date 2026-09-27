"""Il billing del portale B2B su Creem (27/09/2026).

Disegno: la landing vende volumi di crediti al mese (la curva dello
slider). Ogni volume e' un PRODOTTO Creem, creato da Giuseppe nella
dashboard; la mappa volume -> product_id vive in CREEM_PRODOTTI (JSON
nell'env del server, es. {"100000": "prod_abc", ...}).

Flusso: POST /me/checkout {volume} -> POST api.creem.io/v1/checkouts con
metadata.user_id -> l'URL del checkout ospitato. Al pagamento Creem
chiama POST /webhooks/creem: firma HMAC-SHA256 verificata sull'header
creem-signature col webhook secret, evento idempotente (la stessa
consegna puo' arrivare due volte: la deduplica e' per event id in
tabella), e crediti_mensili delle chiavi attive dell'utente sale al
volume comprato.

Le chiavi stanno SOLO in /opt/nivult/.env: CREEM_API_KEY,
CREEM_WEBHOOK_SECRET, CREEM_PRODOTTI. Se mancano, il checkout risponde
503 con garbo e il portale dice "scrivici" — niente mezze integrazioni.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os

import httpx
import psycopg

log = logging.getLogger("nivult.api_clienti.billing")

BASE = "https://api.creem.io/v1"


class BillingNonPronto(Exception):
    """Le chiavi Creem non ci sono ancora: il portale dice «scrivici»."""


def _env(nome: str) -> str:
    v = os.environ.get(nome)
    if not v:
        raise BillingNonPronto(nome)
    return v


def _prodotti() -> dict[str, str]:
    try:
        return json.loads(_env("CREEM_PRODOTTI"))
    except (json.JSONDecodeError, BillingNonPronto) as e:
        raise BillingNonPronto("CREEM_PRODOTTI") from e


def volumi() -> list[int]:
    """I volumi vendibili, per la pagina account. Vuota = non configurato."""
    try:
        return sorted(int(v) for v in _prodotti())
    except BillingNonPronto:
        return []


def checkout(user_id: str, email: str, volume: int,
             success_url: str, cancel_url: str) -> str:
    """L'URL del checkout Creem per il volume scelto."""
    prod = _prodotti().get(str(volume))
    if not prod:
        raise BillingNonPronto(f"nessun prodotto per {volume}")
    r = httpx.post(BASE + "/checkouts", timeout=20,
                   headers={"x-api-key": _env("CREEM_API_KEY")},
                   json={"product_id": prod,
                         "success_url": success_url,
                         "cancel_url": cancel_url,
                         "metadata": {"user_id": user_id, "volume": volume},
                         "customer": {"email": email}})
    if r.status_code not in (200, 201):
        log.warning("creem checkout fallito: %s %s", r.status_code,
                    r.text[:200])
        raise BillingNonPronto(f"creem {r.status_code}")
    url = r.json().get("checkout_url")
    if not url:
        raise BillingNonPronto("checkout senza URL")
    return url


def firma_valida(corpo: bytes, firma: str | None) -> bool:
    """HMAC-SHA256 del corpo col webhook secret, contro l'header."""
    try:
        segreto = _env("CREEM_WEBHOOK_SECRET").encode()
    except BillingNonPronto:
        return False
    if not firma:
        return False
    attesa = hmac.new(segreto, corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(attesa, firma)


def applica_pagamento(dsn: str, evento: dict) -> str:
    """checkout.completed / subscription.paid: i crediti salgono al
    volume comprato. Idempotente per id evento: le riconsegne di Creem
    non contano due volte."""
    eid = str(evento.get("id") or "")
    tipo = str(evento.get("eventType") or evento.get("type") or "")
    if tipo not in ("checkout.completed", "subscription.paid"):
        return "ignorato"
    dati = evento.get("object") or {}
    meta = dati.get("metadata") or {}
    uid = meta.get("user_id")
    volume = int(meta.get("volume") or 0)
    if not uid or not volume or not eid:
        log.warning("creem webhook senza metadata utili: %s", tipo)
        return "metadata mancanti"
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("SET lock_timeout = '10s'")
        if not c.execute("SELECT 1 FROM information_schema.tables "
                         "WHERE table_name = 'creem_eventi'").fetchone():
            c.execute("""CREATE TABLE creem_eventi (
                           id text PRIMARY KEY,
                           ricevuto_at timestamptz NOT NULL DEFAULT now())""")
        gia = c.execute(
            "INSERT INTO creem_eventi (id) VALUES (%s) "
            "ON CONFLICT DO NOTHING", (eid,)).rowcount == 0
        if gia:
            return "duplicato"
        n = c.execute(
            "UPDATE api_chiavi SET crediti_mensili = %s "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (volume, uid)).rowcount
        log.info("creem: %s -> user %s a %s crediti/mese su %d chiavi",
                 tipo, uid, volume, n)
        return f"accreditato ({n} chiavi)"
