"""Livello HTTP dell'API clienti B2B ``/v1``.

Il contratto pubblico usa nomi di campi inglesi; le rimozioni privacy sono
deliberate. L'autenticazione e' separata dall'addebito per non consumare crediti
su errori del cliente, entitlement mancante, file assente o errori del database.
L'addebito precede sempre la risposta e un guasto del contatore chiude con 503,
mai con dati gratuiti.
"""
from __future__ import annotations

import ipaddress
import logging
import math
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

import psycopg
import psycopg_pool
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from nivult.api_clienti import chiavi, dati, opensearch, vivo
from nivult.rate_limit import RateLimiter

router = APIRouter(prefix="/v1", tags=["clienti"])

LIMITE_DEFAULT = 100
LIMITE_MASSIMO = 500
LIMITE_FREE = 25
TIER_FREE_MASSIMO = 1_000
RATE_FREE, BURST_FREE = 2, 5
RATE_PAGANTI, BURST_PAGANTI = 15, 30
NOMI_EXPORT = ("offerte", "aziende")
SOGLIA_BULK = 500_000
SOGLIA_DELTA = 200_000
_rate_limiter = RateLimiter()


def _volume_mensile(cliente: dict) -> int:
    if cliente.get("via") == "rapidapi":
        return 10_000
    if "monthly_tier" in cliente:
        return int(cliente["monthly_tier"])
    try:
        volume = (chiavi.volume_per_utente(str(cliente["user_id"]))
                  if cliente.get("user_id") else int(cliente["crediti_mensili"]))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, "credit counter temporarily unavailable") from exc
    cliente["monthly_tier"] = volume
    return volume


def _e_free(cliente: dict) -> bool:
    return _volume_mensile(cliente) <= TIER_FREE_MASSIMO


def _limite_per_tier(cliente: dict, limite: int) -> int:
    return min(limite, LIMITE_FREE if _e_free(cliente) else LIMITE_MASSIMO)


def _applica_rate_limit(request: Request, cliente: dict) -> None:
    free = _e_free(cliente)
    rate, burst = (RATE_FREE, BURST_FREE) if free else (RATE_PAGANTI, BURST_PAGANTI)
    identita = (f"rapidapi:{cliente.get('utente_rapidapi') or _ip(request) or 'unknown'}"
                if cliente.get("via") == "rapidapi" else f"key:{cliente['id']}")
    ammesso, retry = _rate_limiter.allow(identita, rate, burst)
    if not ammesso:
        raise HTTPException(
            429, f"rate limit exceeded: {rate} requests/second (burst {burst})",
            headers={"Retry-After": str(retry)})


def _cliente(request: Request) -> dict:
    """Autentica e limita il canale senza spendere crediti."""
    segreto = os.environ.get("RAPIDAPI_PROXY_SECRET")
    if segreto and request.headers.get("X-RapidAPI-Proxy-Secret") == segreto:
        cliente = {"id": "rapidapi", "user_id": None, "via": "rapidapi",
                   "utente_rapidapi": request.headers.get("X-RapidAPI-User", "")}
        _applica_rate_limit(request, cliente)
        return cliente
    chiave = request.headers.get("X-Api-Key", "").strip()
    if not chiave:
        raise HTTPException(401, "missing API key: send the X-Api-Key header")
    try:
        cliente = dict(chiavi.verifica(chiave))
        _volume_mensile(cliente)
        _applica_rate_limit(request, cliente)
        return cliente
    except chiavi.ChiaveInvalida:
        raise HTTPException(401, "invalid or revoked API key")
    except chiavi.ContatoreNonDisponibile:
        raise HTTPException(503, "credit counter temporarily unavailable")


def _addebita(request: Request, cliente: dict) -> dict:
    if cliente.get("via") == "rapidapi":
        return cliente
    try:
        return chiavi.addebita_chiave(
            str(cliente["id"]), via="api", azione=request.url.path,
            dettaglio=str(request.url.query)[:200] or None)
    except chiavi.ChiaveInvalida:
        raise HTTPException(401, "invalid or revoked API key")
    except chiavi.CreditiEsauriti as e:
        raise HTTPException(429, detail={
            "error": "monthly and purchased credits exhausted",
            "monthly_credits": e.crediti_mensili,
            "purchased_credits": e.crediti_extra,
            "usage_month": str(e.mese_uso)})
    except chiavi.ContatoreNonDisponibile:
        raise HTTPException(503, "credit counter temporarily unavailable")


def _risposta_export(percorso: str, nome_file: str) -> StreamingResponse:
    return StreamingResponse(
        dati.stream_export_senza_pii(percorso), media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{nome_file}"'})


def _richiedi_volume(cliente: dict, minimo: int, prodotto: str) -> None:
    if cliente.get("via") == "rapidapi":
        return
    if _volume_mensile(cliente) < minimo:
        raise HTTPException(403, f"{prodotto} requires the {minimo:,} "
                                 "credits/month tier")


def _pagina(funzione, filtri: dict, cursore: str | None, limite: int) -> dict:
    try:
        righe, prossimo = funzione(filtri, cursore, limite)
    except psycopg.errors.QueryCanceled as e:
        logging.getLogger(__name__).warning("query API interrotta per timeout")
        raise HTTPException(
            503, "database query timed out; narrow the filters or retry") from e
    except (psycopg.errors.LockNotAvailable, psycopg_pool.PoolTimeout) as e:
        logging.getLogger(__name__).warning("database API non disponibile: %s",
                                            type(e).__name__)
        raise HTTPException(503, "database temporarily unavailable") from e
    except (psycopg.OperationalError, psycopg.InterfaceError) as e:
        logging.getLogger(__name__).exception("database API non disponibile")
        raise HTTPException(503, "database temporarily unavailable") from e
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"data": righe, "next_cursor": prossimo, "count_page": len(righe)}


def _filtri(**coppie) -> dict:
    if coppie.get("ats") == "bundesagentur":
        coppie["ats"] = "bundesanstellung"
    return {k: v for k, v in coppie.items() if v is not None}


def _numero_salario(valore: str | None, campo: str) -> float | None:
    if valore is None:
        return None
    try:
        numero = float(valore)
    except ValueError:
        raise HTTPException(400, f"{campo} must be a number")
    if not math.isfinite(numero) or numero < 0:
        raise HTTPException(400, f"{campo} must be finite and non-negative")
    return numero


def _valida_jobs_v2(source_type: str | None, has_salary: str | None,
                    salary_min: str | None, salary_max: str | None) -> tuple[float | None, float | None]:
    if source_type:
        valori = [v.strip().lower() for v in source_type.split(",") if v.strip()]
        if any(v not in opensearch.SOURCE_TYPES for v in valori):
            raise HTTPException(400, "source_type must use direct, aggregator, "
                                     "staffing or public_board")
    if has_salary is not None and has_salary.strip().lower() not in ("true", "false"):
        raise HTTPException(400, "has_salary must be true or false")
    minimo = _numero_salario(salary_min, "salary_min")
    massimo = _numero_salario(salary_max, "salary_max")
    if minimo is not None and massimo is not None and minimo > massimo:
        raise HTTPException(400, "salary_min cannot exceed salary_max")
    return minimo, massimo


@router.get("/jobs")
def jobs(request: Request, country: str | None = None, category: str | None = None,
         ats: str | None = None, exclude_ats: str | None = None,
         seniority: str | None = None, remote: str | None = None,
         language: str | None = None, q: str | None = None, q_any: str | None = None,
         title: str | None = None, technology: str | None = None,
         source_type: str | None = None, company: str | None = None,
         company_slug: str | None = None, company_domain: str | None = None,
         city: str | None = None, state: str | None = None,
         employment_type: str | None = None, department: str | None = None,
         skills: str | None = None, salary_currency: str | None = None,
         has_salary: str | None = None, salary_min: str | None = None,
         salary_max: str | None = None, posted_after: str | None = None,
         posted_before: str | None = None, dal: str | None = None,
         limit: int = Query(LIMITE_DEFAULT, ge=1, le=LIMITE_MASSIMO),
         cursor: str | None = None, cliente=Depends(_cliente)):
    """Cerca offerte attive; i filtri keyword accettano valori multipli separati
    da virgola. ``exclude_ats`` esclude le sorgenti indicate. ``q`` richiede
    tutti i termini, ``q_any`` almeno una delle massimo 15 keyword; ``title``
    cerca solo nel titolo. I filtri v2 coprono datore, dominio, luogo, salario
    e ``source_type``, con validazione senza addebito."""
    minimo, massimo = _valida_jobs_v2(source_type, has_salary, salary_min, salary_max)
    risposta = _pagina(
        opensearch.offerte if opensearch.attivo() else vivo.offerte,
        _filtri(country=country, category=category, ats=ats, exclude_ats=exclude_ats,
                seniority=seniority, remote=remote, language=language, q=q,
                q_any=q_any, title=title, technology=technology, source_type=source_type,
                company=company, company_slug=company_slug, company_domain=company_domain,
                city=city, state=state, employment_type=employment_type,
                department=department, skills=skills, salary_currency=salary_currency,
                has_salary=(has_salary.strip().lower() == "true"
                            if has_salary is not None else None),
                salary_min=minimo, salary_max=massimo,
                dal=posted_after or dal, posted_before=posted_before),
        cursor, _limite_per_tier(cliente, limit))
    _addebita(request, cliente)
    return risposta


@router.get("/companies")
def companies(request: Request, country: str | None = None,
              industry: str | None = None, q: str | None = None,
              technology: str | None = None, employees_min: int | None = None,
              employees_max: int | None = None,
              limit: int = Query(LIMITE_DEFAULT, ge=1, le=LIMITE_MASSIMO),
              cursor: str | None = None, cliente=Depends(_cliente)):
    risposta = _pagina(
        dati.aziende,
        _filtri(country=country, industry=industry, q=q, technology=technology,
                employees_min=employees_min, employees_max=employees_max),
        cursor, _limite_per_tier(cliente, limit))
    _addebita(request, cliente)
    return risposta


@router.get("/companies/{piattaforma}/{slug}")
def company(request: Request, piattaforma: str, slug: str, cliente=Depends(_cliente)):
    azienda = dati.azienda(piattaforma, slug)
    if azienda is None:
        raise HTTPException(404, "company not found")
    _addebita(request, cliente)
    return azienda


@router.get("/changes")
def changes(request: Request, since: str | None = None, cursor: str | None = None,
            limit: int = Query(LIMITE_DEFAULT, ge=1, le=LIMITE_MASSIMO),
            cliente=Depends(_cliente)):
    if not since or not since.strip():
        raise HTTPException(400, "the 'since' parameter is required: an ISO 8601 date, "
                                 "e.g. 2026-09-01T00:00:00Z")
    try:
        datetime.fromisoformat(
            since.strip()[:-1] + "+00:00" if since.strip().endswith("Z")
            else since.strip())
    except ValueError:
        raise HTTPException(400, f"'since' is not a valid ISO 8601 date: {since!r}")
    _richiedi_volume(cliente, SOGLIA_DELTA, "the delta feed")
    try:
        righe, prossimo = dati.cambiamenti(
            since.strip(), cursor, _limite_per_tier(cliente, limit))
    except dati.DeltaRetentionError as e:
        raise HTTPException(400, detail=e.as_dict())
    except ValueError as e:
        raise HTTPException(400, str(e))
    _addebita(request, cliente)
    return {"data": righe, "next_cursor": prossimo, "count_page": len(righe)}


@router.get("/exports/latest")
def export_latest(request: Request, cliente=Depends(_cliente)):
    stato = dati.stato_export()
    _addebita(request, cliente)
    return stato


@router.get("/exports/latest/{nome}")
def export_file(request: Request, nome: str, cliente=Depends(_cliente)):
    if cliente.get("via") == "rapidapi":
        raise HTTPException(403, "the daily bulk export is not part of the "
                                 "RapidAPI plans — write to hello@nivult.com")
    if nome not in NOMI_EXPORT:
        raise HTTPException(404, "unknown export: allowed values are "
                                 + ", ".join(NOMI_EXPORT))
    _richiedi_volume(cliente, SOGLIA_BULK, "the daily bulk export")
    stato = dati.stato_export()
    percorso = (stato.get("file") or {}).get(nome)
    if not percorso or not os.path.isfile(percorso):
        raise HTTPException(404, f"export '{nome}' is not available today")
    _addebita(request, cliente)
    return _risposta_export(percorso, os.path.basename(percorso))


@router.get("/coverage")
def coverage(request: Request, cliente=Depends(_cliente)):
    risposta = dati.copertura()
    _addebita(request, cliente)
    return risposta


@router.get("/usage")
def usage(request: Request, cliente=Depends(_cliente)):
    if cliente.get("via") == "rapidapi":
        return {"via": "rapidapi",
                "note": "usage quotas are managed by the RapidAPI plan"}
    rec = _addebita(request, cliente)
    mensili = int(rec["crediti_mensili"])
    usati = int(rec.get("usati", rec["usati_mese"]))
    extra = int(rec.get("crediti_extra") or 0)
    residui_mensili = max(0, mensili - usati)
    saldo = residui_mensili + extra
    return {"monthly_credits": mensili, "used": usati,
            "remaining": saldo, "usage_month": str(rec["mese_uso"]),
            "monthly_remaining": residui_mensili, "purchased_credits": extra,
            "balance": saldo, "key_credits_used": rec.get("usati_chiave", usati),
            "account_monthly_credits": mensili}


_DEMO_RX = re.compile(r"^[A-Za-z0-9 .+#&()/'À-ÿ\-]{2,40}$")
_demo_finestra: dict[str, list[float]] = {}


def _ip(request: Request) -> str | None:
    host = (request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else None))
    try:
        return str(ipaddress.ip_address(host)) if host else None
    except ValueError:
        return None


def _demo_gate(request: Request) -> None:
    ora = time.time()
    ip = _ip(request) or "?"
    finestra = [t for t in _demo_finestra.get(ip, []) if ora - t < 60]
    if len(finestra) >= 30:
        raise HTTPException(429, "too fast: try again in a minute")
    finestra.append(ora)
    _demo_finestra[ip] = finestra
    if len(_demo_finestra) > 5000:
        for k in [k for k, v in _demo_finestra.items()
                  if not v or ora - v[-1] >= 60]:
            del _demo_finestra[k]


@router.get("/demo/tecnologie")
def demo_tecnologie(request: Request, q: str = Query(default="")):
    q = q.strip()
    if not _DEMO_RX.fullmatch(q):
        raise HTTPException(400, "caratteri non ammessi nella ricerca")
    _demo_gate(request)
    d = dati.demo_tecnologie(q)
    return {"query": q, "data": d["esempi"], "altre": d["altre"],
            "campione": "demo pubblica: due aziende in chiaro. "
                        "La lista intera e' nella API con chiave."}


@router.get("/demo/stats")
def demo_stats(request: Request):
    _demo_gate(request)
    risp = JSONResponse(dati.demo_stats())
    risp.headers["Cache-Control"] = "no-store"
    return risp


CAMPIONI_EXPORT = {
    "offerte": "/opt/nivult/exports/offerte-attive-campione-ultimo.jsonl.gz",
    "aziende": "/opt/nivult/exports/aziende-segnali-campione-ultimo.jsonl.gz",
}


def _campione_export(tipo: str) -> str | None:
    configurato = CAMPIONI_EXPORT.get(tipo)
    if not configurato:
        return None
    nome = os.path.basename(configurato)
    radice = Path(os.environ.get("EXPORT_DIR") or os.path.dirname(configurato))
    ultimo = radice / nome
    if ultimo.is_file():
        return str(ultimo)
    prefisso = nome.split("-campione-", 1)[0]
    rx = re.compile(r"^" + re.escape(prefisso) +
                    r"-campione-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")
    candidati: list[tuple[str, str]] = []
    try:
        for percorso in radice.iterdir():
            m = rx.fullmatch(percorso.name)
            if not m:
                continue
            try:
                date.fromisoformat(m.group(1))
            except ValueError:
                continue
            if percorso.is_file():
                candidati.append((m.group(1), str(percorso)))
    except OSError:
        return None
    return max(candidati)[1] if candidati else None


@router.get("/demo/sample-export")
@router.get("/demo/export-campione")
def demo_export_campione(
    request: Request,
    tipo: str | None = Query(default=None),
    sample_type: str | None = Query(default=None, alias="type"),
):
    _demo_gate(request)
    if sample_type is not None:
        tipi = {"jobs": "offerte", "companies": "aziende"}
        if sample_type not in tipi:
            raise HTTPException(404, "unknown sample type: use jobs or companies")
        tipo = tipi[sample_type]
    else:
        tipo = tipo or "offerte"
    if tipo not in CAMPIONI_EXPORT:
        raise HTTPException(404, "campione sconosciuto: valori ammessi "
                                 + ", ".join(CAMPIONI_EXPORT))
    percorso = _campione_export(tipo)
    if not percorso:
        raise HTTPException(404, "campione non disponibile oggi")
    return _risposta_export(percorso, f"nivult-campione-{tipo}.jsonl.gz")
