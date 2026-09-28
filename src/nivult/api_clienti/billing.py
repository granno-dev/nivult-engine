"""Il billing del portale B2B su Creem (27/09/2026; ricarica 28/09).

Disegno: UN prodotto one-time "Crediti Nivult" nella dashboard Creem.
Lo slider dell'account e' libero in euro, quindi il checkout nasce con
`custom_price` (in centesimi): il PREZZO lo calcola il server dalla
curva pubblica della landing (ANCORA), mai dal client — chi chiede
1.000.000 di crediti si vede addebitare gli euro giusti perche' la
curva euro<-crediti vive qui. Il volume comprato viaggia nella metadata
del checkout e al pagamento finisce in users.crediti_extra: la ricarica
NON scade e NON si azzera al primo del mese (si spende dopo la
franchigia gratuita, vedi chiavi.py).

Flusso: POST /me/checkout {volume} -> POST api.creem.io/v1/checkouts con
metadata.user_id -> l'URL del checkout ospitato. Al pagamento Creem
chiama POST /webhooks/creem: firma HMAC-SHA256 verificata sull'header
creem-signature col webhook secret, evento idempotente (la stessa
consegna puo' arrivare due volte: la deduplica e' per event id in
tabella), e crediti_extra dell'utente sale del volume comprato.

Le chiavi stanno SOLO in /opt/nivult/.env: CREEM_API_KEY,
CREEM_WEBHOOK_SECRET, CREEM_PRODOTTO (il product id; in alternativa
CREEM_PRODOTTI come mappa JSON, si prende il primo). Se mancano, il
checkout risponde 503 con garbo e il portale dice "scrivici" — niente
mezze integrazioni.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os

import httpx
import psycopg

log = logging.getLogger("nivult.api_clienti.billing")

BASE = "https://api.creem.io/v1"

# La curva della landing (crediti, euro): stessa ANCORE dello slider
# nell'account. Qui si legge al contrario: dai crediti agli euro.
ANCORA = [(10_000, 49), (25_000, 89), (50_000, 149), (100_000, 229),
          (250_000, 399), (500_000, 599), (1_000_000, 899),
          (2_500_000, 1_790), (5_000_000, 2_990)]
MINIMO_EURO = 25   # sotto, le commissioni si mangiano il margine


def euro_per_crediti(crediti: int) -> int:
    """Il prezzo in euro per un volume di crediti, interpolazione
    log-log fra le ancore (identica allo slider, letta al rovescio).
    Arrotondato per eccesso: lo sconto non si regala per arrotondamento."""
    c = max(100, min(int(crediti), ANCORA[-1][0]))
    for c2, p2 in ANCORA:
        if c == c2:
            return max(MINIMO_EURO, p2)   # l'ancora e' il prezzo esatto
    if c <= ANCORA[0][0]:
        e = c / ANCORA[0][0] * ANCORA[0][1]
    else:
        e = ANCORA[-1][1]
        for (c1, p1), (c2, p2) in zip(ANCORA, ANCORA[1:]):
            if c <= c2:
                t = (math.log(c) - math.log(c1)) / (math.log(c2) - math.log(c1))
                e = math.exp(math.log(p1) + t * (math.log(p2) - math.log(p1)))
                break
    return max(MINIMO_EURO, math.ceil(e))


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


def _prodotto() -> str:
    """Il product id dell'UNICO prodotto (il prezzo e' custom_price).
    Si accetta anche la vecchia mappa CREEM_PRODOTTI: primo valore."""
    v = os.environ.get("CREEM_PRODOTTO")
    if v:
        return v
    try:
        mappa = _prodotti()
    except BillingNonPronto:
        mappa = {}
    if mappa:
        return next(iter(mappa.values()))
    raise BillingNonPronto("CREEM_PRODOTTO")


def volumi() -> list[int]:
    """I volumi vendibili, per la pagina account. Vuota = non configurato."""
    try:
        return sorted(int(v) for v in _prodotti())
    except BillingNonPronto:
        return []


def checkout(user_id: str, email: str, volume: int,
             success_url: str, cancel_url: str) -> str:
    """L'URL del checkout Creem per il volume scelto. Il prezzo lo
    decide il server: custom_price = la curva, non il client."""
    euro = euro_per_crediti(volume)
    r = httpx.post(BASE + "/checkouts", timeout=20,
                   headers={"x-api-key": _env("CREEM_API_KEY")},
                   json={"product_id": _prodotto(),
                         "custom_price": euro * 100,
                         "units": 1,
                         "success_url": success_url,
                         "metadata": {"user_id": user_id, "volume": volume,
                                      "euro": euro},
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
    """checkout.completed: la ricarica entra in users.crediti_extra —
    i crediti comprati NON scadono e non si azzerano a fine mese
    (28/09/2026: prima alzavano crediti_mensili, cioe' un abbonamento
    regalato a chi pagava una volta). Idempotente per id evento: le
    riconsegne di Creem non contano due volte."""
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
        # creem_eventi nasce con la migrazione 0067: il ruolo dell'API
        # non ha CREATE TABLE, e mai dovra' averlo (least privilege)
        gia = c.execute(
            "INSERT INTO creem_eventi (id) VALUES (%s) "
            "ON CONFLICT DO NOTHING", (eid,)).rowcount == 0
        if gia:
            return "duplicato"
        n = c.execute(
            "UPDATE users SET crediti_extra = crediti_extra + %s "
            "WHERE id = %s",
            (volume, uid)).rowcount
        log.info("creem: %s -> user %s +%s crediti di ricarica (%d righe)",
                 tipo, uid, volume, n)
        return f"accreditato ({n} utente)"
