"""Billing B2B Creem: abbonamenti, top-up, upgrade, storni e webhook.

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
from datetime import datetime

import httpx
import psycopg
from fastapi import HTTPException

log = logging.getLogger("nivult.api_clienti.billing")

BASE = "https://api.creem.io/v1"
PIANI = {
    "starter": {"crediti": 15_000, "month": 4_900, "year": 49_000},
    "growth": {"crediti": 100_000, "month": 14_900, "year": 149_000},
    "scale": {"crediti": 500_000, "month": 44_900, "year": 449_000},
    "business": {"crediti": 1_500_000, "month": 89_900, "year": 899_000},
}
EVENTI_PAGAMENTO = {"checkout.completed"}
EVENTI_STORNO = {"refund.created", "dispute.created"}
EVENTI_SUB_ATTIVI = {"subscription.active", "subscription.paid", "subscription.update"}
EVENTI_SUB_REVOCA = {
    "subscription.canceled": "canceled", "subscription.expired": "expired",
    "subscription.paused": "paused", "subscription.unpaid": "unpaid",
}


class BillingNonPronto(Exception):
    """Configurazione o risposta Creem non utilizzabile."""


class EventoNonApplicabile(HTTPException):
    def __init__(self, dettaglio: str):
        super().__init__(status_code=422, detail=dettaglio)


def euro_per_crediti(crediti: int) -> int:
    return max(40, math.ceil(int(crediti) / 1000 * 4))


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


def _mappa_abbonamenti() -> dict[str, str]:
    try:
        mappa = json.loads(_env("CREEM_PRODOTTI_ABBONAMENTI"))
    except json.JSONDecodeError as exc:
        raise BillingNonPronto("CREEM_PRODOTTI_ABBONAMENTI") from exc
    if not isinstance(mappa, dict):
        raise BillingNonPronto("CREEM_PRODOTTI_ABBONAMENTI non e' una mappa")
    return {str(k): str(v) for k, v in mappa.items()}


def _prodotto_abbonamento(piano: str, intervallo: str) -> str:
    if piano not in PIANI or intervallo not in ("month", "year"):
        raise HTTPException(400, "unknown plan or billing interval")
    chiave = f"{piano}:{intervallo}"
    try:
        return _mappa_abbonamenti()[chiave]
    except KeyError as exc:
        raise BillingNonPronto(chiave) from exc


def _piano_da_prodotto(dati: dict) -> tuple[str, str]:
    prodotto = dati.get("product") or {}
    if not isinstance(prodotto, dict):
        prodotto = {}
    product_id = str(dati.get("product_id") or prodotto.get("id") or "")
    for chiave, valore in _mappa_abbonamenti().items():
        if valore == product_id:
            piano, intervallo = chiave.split(":", 1)
            return piano, intervallo
    raise EventoNonApplicabile("subscription product non configurato")


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
    if not 10_000 <= volume <= 5_000_000:
        raise EventoNonApplicabile("top-up volume must be between 10,000 and 5,000,000")
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
                             "metadata": {"user_id": user_id, "kind": "topup",
                                          "volume": volume, "euro": euro},
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


def _creem_request(method: str, path: str, payload: dict) -> dict:
    try:
        r = httpx.request(method, BASE + path, timeout=20,
                          headers={"x-api-key": _env("CREEM_API_KEY")}, json=payload)
    except httpx.HTTPError as exc:
        raise BillingNonPronto("creem non raggiungibile") from exc
    if not 200 <= r.status_code < 300:
        log.warning("creem API fallita: %s %s", r.status_code, r.text[:200])
        raise BillingNonPronto(f"creem {r.status_code}")
    try:
        return r.json()
    except ValueError as exc:
        raise BillingNonPronto("risposta Creem non JSON") from exc


def checkout_abbonamento(user_id: str, email: str, piano: str, intervallo: str,
                         success_url: str, cancel_url: str) -> str:
    product_id = _prodotto_abbonamento(piano, intervallo)
    request_id = "nivult-abbonamento-" + secrets.token_urlsafe(18)
    risposta = _creem_request("POST", "/checkouts", {
        "product_id": product_id, "request_id": request_id,
        "success_url": success_url,
        "metadata": {"user_id": user_id, "kind": "subscription",
                     "subscription_plan": piano, "subscription_interval": intervallo},
        "customer": {"email": email}})
    url = risposta.get("checkout_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BillingNonPronto("checkout senza URL valido")
    return url


def upgrade_abbonamento(subscription_id: str, piano: str, intervallo: str) -> dict:
    if not subscription_id:
        raise BillingNonPronto("subscription mancante")
    return _creem_request("POST", f"/subscriptions/{subscription_id}/upgrade",
                          {"product_id": _prodotto_abbonamento(piano, intervallo),
                           "update_behavior": "proration-charge-immediately"})


def customer_portal(customer_id: str) -> str:
    if not customer_id:
        raise BillingNonPronto("customer mancante")
    risposta = _creem_request("POST", "/customers/billing",
                              {"customer_id": customer_id})
    url = risposta.get("customer_portal_link")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BillingNonPronto("customer portal senza URL valido")
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
        (user_id, azione, json.dumps(dettaglio, separators=(",", ":"),
                                  sort_keys=True, default=str)))


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


def _timestamp(valore):
    if not valore:
        return None
    try:
        return datetime.fromisoformat(str(valore).replace("Z", "+00:00"))
    except ValueError as exc:
        raise EventoNonApplicabile("data subscription non valida") from exc


def _dati_subscription(dati: dict) -> dict:
    subscription_id = str(dati.get("id") or "").strip()
    if not subscription_id:
        raise EventoNonApplicabile("subscription id mancante")
    customer = _dict(dati.get("customer"), "subscription customer")
    customer_id = str(customer.get("id") or "").strip()
    if not customer_id:
        raise EventoNonApplicabile("customer id mancante")
    metadata = dati.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    user_id = metadata.get("user_id")
    last_transaction = dati.get("last_transaction") or {}
    if not isinstance(last_transaction, dict):
        last_transaction = {}
    transaction_id = (dati.get("last_transaction_id") or last_transaction.get("id"))
    start_raw = (dati.get("current_period_start_date")
                 or dati.get("current_period_start"))
    end_raw = dati.get("current_period_end_date") or dati.get("current_period_end")
    return {
        "subscription_id": subscription_id, "customer_id": customer_id,
        "user_id": _user_id(user_id) if user_id else None,
        "transaction_id": str(transaction_id) if transaction_id else None,
        "period_start": _timestamp(start_raw), "period_end": _timestamp(end_raw),
        "state_key": str(transaction_id or start_raw or end_raw or "initial"),
    }


def _utente_subscription(cur, refs: dict) -> str:
    user_id = refs.get("user_id")
    if not user_id:
        cur.execute("SELECT id::text FROM users WHERE creem_customer_id = %s",
                    (refs["customer_id"],))
        riga = cur.fetchone()
        if riga is None:
            raise EventoNonApplicabile("utente non trovato per subscription")
        user_id = riga[0]
    _lock_utente(cur, user_id)
    return user_id


def _quota_subscription(cur, user_id: str, crediti: int, reset: bool) -> None:
    if reset:
        cur.execute("UPDATE api_chiavi SET crediti_mensili = %s, usati_mese = 0, "
                    "mese_uso = CURRENT_DATE WHERE user_id = %s", (crediti, user_id))
    else:
        cur.execute("UPDATE api_chiavi SET crediti_mensili = %s WHERE user_id = %s",
                    (crediti, user_id))


def _attiva_subscription(cur, evento: dict, dati: dict) -> str:
    piano, intervallo = _piano_da_prodotto(dati)
    refs = _dati_subscription(dati)
    user_id = _utente_subscription(cur, refs)
    cur.execute("SELECT creem_period_start FROM users WHERE id = %s", (user_id,))
    precedente = cur.fetchone()[0]
    cur.execute("INSERT INTO creem_subscription_eventi "
                "(subscription_id, state_key, event_id) VALUES (%s, %s, %s) "
                "ON CONFLICT DO NOTHING RETURNING event_id",
                (refs["subscription_id"], refs["state_key"], evento["id"]))
    nuovo_stato = cur.fetchone() is not None
    cambio_periodo = (precedente is None or
                      (refs["period_start"] is not None
                       and refs["period_start"] != precedente))
    cur.execute(
        "UPDATE users SET subscription_status = 'active', subscription_plan = %s, "
        "subscription_interval = %s, current_period_end = COALESCE(%s, current_period_end), "
        "creem_customer_id = %s, creem_subscription_id = %s, "
        "creem_period_start = COALESCE(%s, creem_period_start), "
        "creem_last_transaction_id = COALESCE(%s, creem_last_transaction_id) "
        "WHERE id = %s",
        (piano, intervallo, refs["period_end"], refs["customer_id"],
         refs["subscription_id"], refs["period_start"], refs["transaction_id"], user_id))
    _quota_subscription(cur, user_id, PIANI[piano]["crediti"],
                        reset=nuovo_stato and cambio_periodo)
    _audit(cur, user_id, "subscription-attiva",
           {"subscription_id": refs["subscription_id"], "state_key": refs["state_key"],
            "plan": piano, "interval": intervallo,
            "reset": nuovo_stato and cambio_periodo})
    return "subscription attiva"


def _scheduled_cancel_subscription(cur, dati: dict) -> str:
    refs = _dati_subscription(dati)
    user_id = _utente_subscription(cur, refs)
    cur.execute("UPDATE users SET subscription_status = 'scheduled_cancel', "
                "current_period_end = COALESCE(%s, current_period_end), "
                "creem_customer_id = %s, creem_subscription_id = %s WHERE id = %s",
                (refs["period_end"], refs["customer_id"], refs["subscription_id"], user_id))
    _audit(cur, user_id, "subscription-scheduled-cancel",
           {"subscription_id": refs["subscription_id"],
            "current_period_end": refs["period_end"]})
    return "cancellazione programmata"


def _revoca_subscription(cur, dati: dict, stato: str) -> str:
    refs = _dati_subscription(dati)
    user_id = _utente_subscription(cur, refs)
    cur.execute("UPDATE users SET subscription_status = %s, "
                "current_period_end = COALESCE(%s, current_period_end), "
                "creem_customer_id = %s, creem_subscription_id = %s WHERE id = %s",
                (stato, refs["period_end"], refs["customer_id"],
                 refs["subscription_id"], user_id))
    cur.execute("UPDATE api_chiavi SET crediti_mensili = 0 WHERE user_id = %s",
                (user_id,))
    _audit(cur, user_id, "subscription-revoca",
           {"subscription_id": refs["subscription_id"], "status": stato})
    return "subscription revocata"


def applica_pagamento(dsn: str, evento: dict) -> str:
    """Applica un evento Creem; duplicati e retry non duplicano mai gli effetti."""
    if not isinstance(evento, dict):
        raise EventoNonApplicabile("evento non e' un oggetto JSON")
    tipo = str(evento.get("eventType") or evento.get("type") or "")
    eventi = (EVENTI_PAGAMENTO | EVENTI_STORNO | EVENTI_SUB_ATTIVI
              | set(EVENTI_SUB_REVOCA) | {"subscription.scheduled_cancel"})
    if tipo not in eventi:
        return "ignorato"
    dati = _dict(evento.get("object"), "object evento")
    metadata = dati.get("metadata") or {}
    if (tipo == "checkout.completed" and isinstance(metadata, dict)
            and metadata.get("kind") == "subscription"):
        return "ignorato-abbonamento"
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
            if tipo in EVENTI_PAGAMENTO:
                return _accredita(cur, evento, dati)
            if tipo in EVENTI_SUB_ATTIVI:
                return _attiva_subscription(cur, evento, dati)
            if tipo == "subscription.scheduled_cancel":
                return _scheduled_cancel_subscription(cur, dati)
            if tipo in EVENTI_SUB_REVOCA:
                return _revoca_subscription(cur, dati, EVENTI_SUB_REVOCA[tipo])
            return _storna(cur, evento, dati)
    except EventoNonApplicabile as exc:
        log.warning("creem evento %s (%s) da riconciliare: %s", eid, tipo, exc.detail)
        raise
    except BillingNonPronto as exc:
        raise HTTPException(503, "configurazione billing non disponibile") from exc
    except psycopg.Error as exc:
        log.warning("creem evento %s (%s) non committato: %s",
                    eid, tipo, type(exc).__name__)
        raise HTTPException(status_code=503,
                            detail="billing temporaneamente non disponibile") from exc
