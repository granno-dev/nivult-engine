"""Billing B2B su Creem: checkout one-time, ricariche e storni.

Il prezzo e il volume sono decisi dal server. I webhook sono verificati con
HMAC-SHA256 e applicati in UNA transazione: evento, saldo e storico committano
o falliscono insieme. I tipi gestiti sono quelli documentati da Creem:
``checkout.completed``, ``subscription.paid``, ``refund.created`` e
``dispute.created``. Il request_id del checkout collega gli eventi successivi
senza indovinare account da email o importi.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import secrets
import uuid

import httpx
import psycopg
from fastapi import HTTPException

log = logging.getLogger("nivult.api_clienti.billing")

BASE = "https://api.creem.io/v1"
ANCORA = [(10_000, 49), (25_000, 89), (50_000, 149), (100_000, 229),
          (250_000, 399), (500_000, 599), (1_000_000, 899),
          (2_500_000, 1_790), (5_000_000, 2_990)]
MINIMO_EURO = 25
EVENTI_PAGAMENTO = {"checkout.completed", "subscription.paid"}
EVENTI_STORNO = {"refund.created", "dispute.created"}


class BillingNonPronto(Exception):
    """Configurazione o risposta Creem non utilizzabile."""


class EventoNonApplicabile(HTTPException):
    def __init__(self, dettaglio: str):
        super().__init__(status_code=422, detail=dettaglio)


def euro_per_crediti(crediti: int) -> int:
    c = max(100, min(int(crediti), ANCORA[-1][0]))
    for c2, p2 in ANCORA:
        if c == c2:
            return max(MINIMO_EURO, p2)
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


def _env(nome: str) -> str:
    v = os.environ.get(nome)
    if not v:
        raise BillingNonPronto(nome)
    return v


def _prodotti() -> dict[str, str]:
    try:
        mappa = json.loads(_env("CREEM_PRODOTTI"))
    except (json.JSONDecodeError, BillingNonPronto) as e:
        raise BillingNonPronto("CREEM_PRODOTTI") from e
    if not isinstance(mappa, dict):
        raise BillingNonPronto("CREEM_PRODOTTI non e' una mappa")
    return mappa


def _prodotto() -> str:
    v = os.environ.get("CREEM_PRODOTTO")
    if v:
        return v
    try:
        mappa = _prodotti()
    except BillingNonPronto:
        mappa = {}
    if mappa:
        return str(next(iter(mappa.values())))
    raise BillingNonPronto("CREEM_PRODOTTO")


def volumi() -> list[int]:
    try:
        return sorted(int(v) for v in _prodotti())
    except (BillingNonPronto, TypeError, ValueError):
        return []


def _volume(v) -> int:
    if isinstance(v, bool):
        raise EventoNonApplicabile("volume crediti non valido")
    try:
        volume = int(v)
    except (TypeError, ValueError, OverflowError) as exc:
        raise EventoNonApplicabile("volume crediti non valido") from exc
    if isinstance(v, float) and not v.is_integer():
        raise EventoNonApplicabile("volume crediti non valido")
    if str(v).strip() != str(volume) and not isinstance(v, int):
        raise EventoNonApplicabile("volume crediti non valido")
    if not 100 <= volume <= 5_000_000:
        raise EventoNonApplicabile("volume crediti fuori dalla curva vendibile")
    return volume


def _user_id(v) -> str:
    try:
        return str(uuid.UUID(str(v)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise EventoNonApplicabile("user_id non valido nei metadata") from exc


def checkout(user_id: str, email: str, volume: int,
             success_url: str, cancel_url: str) -> str:
    try:
        volume = _volume(volume)
    except EventoNonApplicabile as exc:
        raise HTTPException(status_code=400, detail=exc.detail) from exc
    euro = euro_per_crediti(volume)
    request_id = "nivult-crediti-" + secrets.token_urlsafe(18)
    try:
        r = httpx.post(BASE + "/checkouts", timeout=20,
                       headers={"x-api-key": _env("CREEM_API_KEY")},
                       json={"product_id": _prodotto(), "request_id": request_id,
                             "custom_price": euro * 100, "units": 1,
                             "success_url": success_url,
                             "metadata": {"user_id": user_id, "volume": volume,
                                          "euro": euro},
                             "customer": {"email": email}})
    except httpx.HTTPError as exc:
        log.warning("creem checkout non raggiungibile: %s", type(exc).__name__)
        raise BillingNonPronto("creem non raggiungibile") from exc
    if r.status_code not in (200, 201):
        log.warning("creem checkout fallito: %s %s", r.status_code, r.text[:200])
        raise BillingNonPronto(f"creem {r.status_code}")
    try:
        url = r.json().get("checkout_url")
    except (ValueError, AttributeError) as exc:
        raise BillingNonPronto("checkout con risposta non JSON") from exc
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BillingNonPronto("checkout senza URL valido")
    return url


def firma_valida(corpo: bytes, firma: str | None) -> bool:
    try:
        segreto = _env("CREEM_WEBHOOK_SECRET").encode()
    except BillingNonPronto:
        return False
    if not firma:
        return False
    attesa = hmac.new(segreto, corpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(attesa, firma)


def _dict(v, nome: str) -> dict:
    if not isinstance(v, dict):
        raise EventoNonApplicabile(f"{nome} mancante o malformato")
    return v


def _metadata_pagamento(dati: dict) -> tuple[str, int]:
    meta = _dict(dati.get("metadata"), "metadata checkout")
    return _user_id(meta.get("user_id")), _volume(meta.get("volume"))


def _riferimenti(dati: dict, checkout_diretto: bool = False) -> dict[str, str]:
    checkout = dati if checkout_diretto else dati.get("checkout") or {}
    if not isinstance(checkout, dict):
        checkout = {}
    ordine = dati.get("order") or checkout.get("order") or {}
    if not isinstance(ordine, dict):
        ordine = {}
    transazione = dati.get("transaction") or {}
    if not isinstance(transazione, dict):
        transazione = {}
    refs = {
        "checkout_id": checkout.get("id"),
        "request_id": checkout.get("request_id"),
        "order_id": ordine.get("id") or transazione.get("order"),
        "transaction_id": (transazione.get("id") or ordine.get("transaction")
                           or dati.get("last_transaction_id")),
    }
    return {k: str(v) for k, v in refs.items()
            if isinstance(v, (str, int)) and str(v)}


def _condizione_refs(refs: dict[str, str]) -> tuple[str, list[str]]:
    clausole = []
    valori = []
    for campo in ("checkout_id", "request_id", "order_id", "transaction_id"):
        if refs.get(campo):
            clausole.append(f"dettaglio::jsonb->>'{campo}' = %s")
            valori.append(refs[campo])
    return ("(" + " OR ".join(clausole) + ")" if clausole else "FALSE"), valori


def _audit(cur, user_id: str, azione: str, dettaglio: dict) -> None:
    cur.execute(
        "INSERT INTO portale_uso (user_id, via, azione, dettaglio, crediti) "
        "VALUES (%s, 'billing', %s, %s, 0)",
        (user_id, azione, json.dumps(dettaglio, separators=(",", ":"), sort_keys=True)))


def _importo_valuta(dati: dict, storno: bool = False) -> tuple[int | None, str | None]:
    if storno:
        importo = dati.get("refund_amount") if "refund_amount" in dati else dati.get("amount")
        valuta = dati.get("refund_currency") or dati.get("currency")
    else:
        ordine = dati.get("order") or {}
        if not isinstance(ordine, dict):
            ordine = {}
        importo, valuta = ordine.get("amount"), ordine.get("currency")
    if isinstance(importo, bool) or not isinstance(importo, (int, float)):
        importo = None
    elif isinstance(importo, float) and not importo.is_integer():
        importo = None
    elif importo < 0:
        importo = None
    else:
        importo = int(importo)
    return importo, valuta if isinstance(valuta, str) else None


def _lock_utente(cur, user_id: str) -> tuple[int, str]:
    cur.execute("SELECT crediti_extra, status FROM users WHERE id = %s FOR UPDATE",
                (user_id,))
    r = cur.fetchone()
    if r is None or r[1] == "deleted":
        raise EventoNonApplicabile("utente assente o cancellato: riconciliazione richiesta")
    return int(r[0]), str(r[1])


def _inserisci_storico(cur, user_id: str, crediti: int, importo: int | None,
                       valuta: str | None, evento_id: str) -> None:
    cur.execute(
        "INSERT INTO portale_acquisti (user_id, crediti, importo_cent, valuta, evento_id) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (user_id, crediti, importo, valuta, evento_id))
    if cur.fetchone() is None:
        raise EventoNonApplicabile("storico finanziario in conflitto")


def _accredita(cur, evento: dict, dati: dict) -> str:
    user_id, volume = _metadata_pagamento(dati)
    _lock_utente(cur, user_id)
    cur.execute("UPDATE users SET crediti_extra = crediti_extra + %s "
                "WHERE id = %s AND status <> 'deleted' RETURNING crediti_extra",
                (volume, user_id))
    if cur.fetchone() is None:
        raise EventoNonApplicabile("accredito non applicato: utente non valido")
    importo, valuta = _importo_valuta(dati)
    dettaglio = _riferimenti(dati, evento["eventType"] == "checkout.completed")
    if not dettaglio:
        raise EventoNonApplicabile("pagamento senza riferimenti riconciliabili")
    _inserisci_storico(cur, user_id, volume, importo, valuta, str(evento["id"]))
    dettaglio["volume"] = volume
    _audit(cur, user_id, "acquisto", dettaglio)
    return "accreditato (1 utente)"


def _acquisto_origine(cur, refs: dict[str, str]) -> tuple[str, int] | None:
    condizione, valori = _condizione_refs(refs)
    cur.execute(
        "SELECT user_id::text, dettaglio FROM portale_uso "
        "WHERE via = 'billing' AND azione = 'acquisto' AND " + condizione +
        " ORDER BY at DESC LIMIT 2", valori)
    righe = cur.fetchall()
    if not righe:
        return None
    if len(righe) > 1:
        raise EventoNonApplicabile("riferimento pagamento ambiguo")
    try:
        dettaglio = json.loads(righe[0][1])
        return _user_id(righe[0][0]), _volume(dettaglio["volume"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        raise EventoNonApplicabile("audit acquisto originale illeggibile") from exc


def _revocati_in_precedenza(cur, refs: dict[str, str]) -> int:
    condizione, valori = _condizione_refs(refs)
    cur.execute(
        "SELECT dettaglio FROM portale_uso WHERE via = 'billing' "
        "AND azione IN ('rimborso', 'disputa') AND " + condizione, valori)
    totale = 0
    for (grezzo,) in cur.fetchall():
        try:
            totale += max(0, int(json.loads(grezzo).get("revocati") or 0))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise EventoNonApplicabile("audit storno precedente illeggibile") from exc
    return totale


def _storna(cur, evento: dict, dati: dict) -> str:
    tipo = evento["eventType"]
    refs = _riferimenti(dati)
    if not refs:
        raise EventoNonApplicabile("storno senza riferimenti al pagamento originale")
    origine = _acquisto_origine(cur, refs)
    if origine is None:
        checkout = _dict(dati.get("checkout"), "checkout dello storno")
        origine = _metadata_pagamento(checkout)
    user_id, volume = origine
    extra, _ = _lock_utente(cur, user_id)
    gia = _revocati_in_precedenza(cur, refs)
    ancora_revocabili = max(0, volume - gia)
    revocati = min(extra, ancora_revocabili)
    if tipo == "dispute.created":
        cur.execute("UPDATE users SET crediti_extra = crediti_extra - %s, "
                    "status = 'paused' WHERE id = %s AND status <> 'deleted' "
                    "RETURNING crediti_extra", (revocati, user_id))
    else:
        cur.execute("UPDATE users SET crediti_extra = crediti_extra - %s "
                    "WHERE id = %s AND status <> 'deleted' RETURNING crediti_extra",
                    (revocati, user_id))
    if cur.fetchone() is None:
        raise EventoNonApplicabile("storno non applicato: utente non valido")
    importo, valuta = _importo_valuta(dati, storno=True)
    _inserisci_storico(cur, user_id, -revocati,
                       -importo if importo is not None else None,
                       valuta, str(evento["id"]))
    dettaglio = dict(refs)
    dettaglio.update({"volume": volume, "revocati": revocati})
    _audit(cur, user_id, "disputa" if tipo == "dispute.created" else "rimborso", dettaglio)
    if tipo == "dispute.created":
        return f"disputa applicata ({revocati} crediti revocati; account sospeso)"
    return f"rimborso applicato ({revocati} crediti revocati)"


def applica_pagamento(dsn: str, evento: dict) -> str:
    """Applica un evento Creem; duplicati e retry non duplicano mai gli effetti."""
    if not isinstance(evento, dict):
        raise EventoNonApplicabile("evento non e' un oggetto JSON")
    tipo = str(evento.get("eventType") or evento.get("type") or "")
    if tipo not in EVENTI_PAGAMENTO | EVENTI_STORNO:
        return "ignorato"
    eid = str(evento.get("id") or "").strip()
    if not eid:
        raise EventoNonApplicabile("event id mancante")
    evento = dict(evento)
    evento["id"], evento["eventType"] = eid, tipo
    try:
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '10s'")
            cur.execute("INSERT INTO creem_eventi (id) VALUES (%s) "
                        "ON CONFLICT DO NOTHING RETURNING id", (eid,))
            if cur.fetchone() is None:
                return "duplicato"
            dati = _dict(evento.get("object"), "object evento")
            if tipo in EVENTI_PAGAMENTO:
                return _accredita(cur, evento, dati)
            return _storna(cur, evento, dati)
    except EventoNonApplicabile as exc:
        log.warning("creem evento %s (%s) da riconciliare: %s", eid, tipo, exc.detail)
        raise
    except psycopg.Error as exc:
        log.warning("creem evento %s (%s) non committato: %s",
                    eid, tipo, type(exc).__name__)
        raise HTTPException(status_code=503,
                            detail="billing temporaneamente non disponibile") from exc
