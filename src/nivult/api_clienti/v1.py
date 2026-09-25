"""Il livello HTTP dell'API clienti B2B, versionata nel percorso come /v1.

LA REGOLA DELL'API: i campi delle risposte non si rinominano e non si
tolgono MAI — si aggiungono soltanto. Chi compra il dataset ci scrive
codice sopra: un campo rinominato e' un incidente di produzione loro,
non un refactor nostro. Per questo il router passa i dict del data layer
cosi' come sono, senza rimappare nulla.

Qui c'e' SOLO HTTP: parametri, codici di stato, autenticazione. I dati
arrivano da `nivult.api_clienti.dati` (modulo a parte, stessa interfaccia
per tutti gli endpoint), le chiavi da `nivult.api_clienti.chiavi`.

Contratto atteso da `dati.stato_export()`:
    {"data": "2026-09-23", "righe": {...},
     "file": {"offerte": "/percorso/offerte-....jsonl.gz",
              "aziende": "/percorso/aziende-....jsonl.gz"}}
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from nivult.api_clienti import chiavi, dati

router = APIRouter(prefix="/v1", tags=["clienti"])

# Oltre le 500 righe una pagina non e' piu' una pagina: e' un export
# mascherato da chiamata, e gli export hanno la loro rotta.
LIMITE_DEFAULT = 100
LIMITE_MASSIMO = 500

# I soli due export del giorno che si possono scaricare.
NOMI_EXPORT = ("offerte", "aziende")


def _cliente(request: Request) -> dict:
    """La dipendenza di autenticazione: header X-Api-Key -> record chiave.

    La chiave viaggia SOLO in un header: mai in query string, perche' le
    URL finiscono nei log — la stessa regola dei token di sessione.
    Ogni chiamata che supera questo cancello costa un credito.
    """
    chiave = request.headers.get("X-Api-Key", "").strip()
    if not chiave:
        raise HTTPException(401, "chiave API mancante: serve l'header X-Api-Key")
    try:
        return chiavi.autentica(chiave)
    except chiavi.ChiaveInvalida:
        raise HTTPException(401, "chiave API non valida o revocata")
    except chiavi.CreditiEsauriti as e:
        raise HTTPException(429, detail={
            "errore": "crediti mensili esauriti",
            "crediti_mensili": e.crediti_mensili,
            "mese_uso": str(e.mese_uso)})


def _pagina(funzione, filtri: dict, cursore: str | None, limite: int) -> dict:
    """La forma delle liste, una sola per tutte: data / next_cursor / count_page."""
    righe, prossimo = funzione(filtri, cursore, limite)
    return {"data": righe, "next_cursor": prossimo, "count_page": len(righe)}


def _filtri(**coppie) -> dict:
    """Solo i parametri presenti: al data layer non arrivano i None."""
    return {k: v for k, v in coppie.items() if v is not None}


@router.get("/jobs")
def jobs(country: str | None = None, category: str | None = None,
         ats: str | None = None, seniority: str | None = None,
         remote: str | None = None, language: str | None = None,
         q: str | None = None, technology: str | None = None,
         dal: str | None = None,
         limit: int = Query(LIMITE_DEFAULT, ge=1, le=LIMITE_MASSIMO),
         cursor: str | None = None, _=Depends(_cliente)):
    """Le offerte, filtrate e paginate a cursore."""
    return _pagina(dati.offerte,
                   _filtri(country=country, category=category, ats=ats,
                           seniority=seniority, remote=remote,
                           language=language, q=q, technology=technology,
                           dal=dal),
                   cursor, limit)


@router.get("/companies")
def companies(country: str | None = None, industry: str | None = None,
              q: str | None = None, technology: str | None = None,
              employees_min: int | None = None,
              employees_max: int | None = None,
              limit: int = Query(LIMITE_DEFAULT, ge=1, le=LIMITE_MASSIMO),
              cursor: str | None = None, _=Depends(_cliente)):
    """Le aziende, filtrate e paginate a cursore."""
    return _pagina(dati.aziende,
                   _filtri(country=country, industry=industry, q=q,
                           technology=technology,
                           employees_min=employees_min,
                           employees_max=employees_max),
                   cursor, limit)


@router.get("/companies/{piattaforma}/{slug}")
def company(piattaforma: str, slug: str, _=Depends(_cliente)):
    """La singola azienda, technologies comprese."""
    azienda = dati.azienda(piattaforma, slug)
    if azienda is None:
        raise HTTPException(404, "azienda non trovata")
    return azienda


@router.get("/changes")
def changes(since: str | None = None, cursor: str | None = None,
            limit: int = Query(LIMITE_DEFAULT, ge=1, le=LIMITE_MASSIMO),
            _=Depends(_cliente)):
    """Il delta feed: offerte nuove e chiuse da `since` in poi.

    `since` e' OBBLIGATORIO: senza, «tutto cio' che e' cambiato» vorrebbe
    dire tutto il corpus, e non e' una domanda a cui questa rotta risponde.
    """
    if not since or not since.strip():
        raise HTTPException(
            400, "parametro 'since' obbligatorio: una data ISO 8601, "
                 "es. 2026-09-01T00:00:00Z")
    try:
        # fromisoformat non accetta la «Z» prima di Python 3.11: si traduce.
        datetime.fromisoformat(
            since.strip()[:-1] + "+00:00" if since.strip().endswith("Z")
            else since.strip())
    except ValueError:
        raise HTTPException(
            400, f"'since' non e' una data ISO 8601 valida: {since!r}")
    righe, prossimo = dati.cambiamenti(since.strip(), cursor, limit)
    return {"data": righe, "next_cursor": prossimo, "count_page": len(righe)}


@router.get("/exports/latest")
def export_latest(_=Depends(_cliente)):
    """Lo stato dell'export del giorno: data, righe e path dei file."""
    return dati.stato_export()


@router.get("/exports/latest/{nome}")
def export_file(nome: str, _=Depends(_cliente)):
    """Il file .jsonl.gz del giorno. Il nome e' chiuso: niente path dal client."""
    if nome not in NOMI_EXPORT:
        raise HTTPException(404, "export sconosciuto: valori ammessi "
                            + ", ".join(NOMI_EXPORT))
    stato = dati.stato_export()
    percorso = (stato.get("file") or {}).get(nome)
    if not percorso or not os.path.isfile(percorso):
        raise HTTPException(404, f"export '{nome}' non disponibile oggi")
    return FileResponse(percorso, media_type="application/gzip",
                        filename=os.path.basename(percorso))


@router.get("/coverage")
def coverage(_=Depends(_cliente)):
    """Il manifest della copertura per campo.

    E' l'argomento di vendita dichiarato: i buchi si mostrano, non si
    nascondono. Passa com'e' dal data layer — niente abbellimenti qui.
    """
    return dati.copertura()


@router.get("/usage")
def usage(cliente=Depends(_cliente)):
    """I crediti della chiave che sta chiamando: mensili, usati, residui.

    Questa chiamata stessa costa un credito, quindi `usati` la include.
    """
    mensili = cliente["crediti_mensili"]
    usati = cliente["usati_mese"]
    return {"crediti_mensili": mensili, "usati": usati,
            "residui": max(0, mensili - usati),
            "mese_uso": str(cliente["mese_uso"])}


# ── la demo pubblica della vetrina (25/09/2026) ──────────────────────
# Senza chiave, ma non senza difese: whitelist di caratteri (l'iniezione
# SQL e' impossibile per costruzione), otto righe al massimo, trenta
# chiamate al minuto per IP, e solo i campi che la pagina mostra.
_DEMO_RX = re.compile(r"^[A-Za-z0-9 .+#&()/'À-ÿ\-]{2,40}$")
_demo_finestra: dict[str, list[float]] = {}


@router.get("/demo/tecnologie")
def demo_tecnologie(request: Request, q: str = Query(default="")):
    """Quali aziende assumono con la tecnologia q — la demo della landing.

    Risponde anche senza chiave perche' e' la porta d'ingresso pubblica:
    i campi restituiti sono quelli della vetrina, niente di piu'."""
    q = q.strip()
    if not _DEMO_RX.fullmatch(q):
        raise HTTPException(400, "caratteri non ammessi nella ricerca")
    ora = time.time()
    ip = request.client.host if request.client else "?"
    finestra = [t for t in _demo_finestra.get(ip, []) if ora - t < 60]
    if len(finestra) >= 30:
        raise HTTPException(429, "troppo veloce: riprova tra un minuto")
    finestra.append(ora)
    _demo_finestra[ip] = finestra
    d = dati.demo_tecnologie(q)
    return {"query": q, "data": d["esempi"],
            "totale_aziende": d["totale_aziende"],
            "campione": "demo pubblica: 2 aziende su %d. La lista intera e' nella API con chiave." % d["totale_aziende"]}
