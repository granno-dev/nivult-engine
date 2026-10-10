"""Microsoft Graph, contesto applicazione: il calendario di Giuseppe.

Nato il 10/10/2026 per nivult.com/booking: la pagina raccoglie lo slot,
qui si crea l'evento Teams vero sul calendario business. App-only
(client credentials) con permesso Calendars.ReadWrite — nessun utente
loggato, nessun refresh token: il demone e' la nostra API stessa.

L'invito al lead NON lo mandiamo noi: lo spedisce Outlook quando
l'evento con attendee viene creato, e lo annulla quando lo cancelliamo.
E' il motivo per cui le nostre mail di conferma non contengono il link
Teams: quello arriva dal calendario, canale che i filtri conoscono.

Configurazione: MS_BOOKING_TENANT_ID, MS_BOOKING_CLIENT_ID,
MS_BOOKING_CLIENT_SECRET, MS_BOOKING_ORGANIZER (UPN del calendario).
Senza queste il modulo rifiuta di partire — meglio un errore chiaro
che un 500 misterioso dalla pagina pubblica.
"""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import httpx

GRAPH = "https://graph.microsoft.com/v1.0"
FUSO = "Europe/Rome"

_token_lock = threading.Lock()
_token: dict = {"valore": "", "scade": 0.0}


def _env(nome: str) -> str:
    v = os.environ.get(nome, "").strip()
    if not v:
        raise RuntimeError(f"Microsoft Graph non configurato: manca {nome}")
    return v


def _token_app() -> str:
    """Client credentials con cache: il token dura ~1h, lo chiediamo una
    volta per processo. Il margine di 60s copre la latenza della chiamata
    che lo usa."""
    with _token_lock:
        if time.time() < _token["scade"]:
            return _token["valore"]
        r = httpx.post(
            f"https://login.microsoftonline.com/{_env('MS_BOOKING_TENANT_ID')}/oauth2/v2.0/token",
            data={
                "client_id": _env("MS_BOOKING_CLIENT_ID"),
                "client_secret": _env("MS_BOOKING_CLIENT_SECRET"),
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
            timeout=15,
        )
        if r.status_code != 200:
            raise RuntimeError(f"token Graph rifiutato ({r.status_code}): "
                               f"{r.text[:200]}")
        d = r.json()
        _token["valore"] = d["access_token"]
        _token["scade"] = time.time() + int(d.get("expires_in", 3600)) - 60
        return _token["valore"]


def _graph(metodo: str, percorso: str, **kw) -> httpx.Response:
    r = httpx.request(metodo, f"{GRAPH}{percorso}",
                      headers={"Authorization": f"Bearer {_token_app()}"},
                      timeout=20, **kw)
    if r.status_code >= 400:
        raise RuntimeError(f"Graph {metodo} {percorso} ({r.status_code}): "
                           f"{r.text[:300]}")
    return r


def _organizer() -> str:
    return _env("MS_BOOKING_ORGANIZER")


def _interno() -> str:
    """Chi parla davvero alla call (Giuseppe). L'organizer e' la casella
    pubblica (hello@), ma la disponibilita' e la presenza sono le sue."""
    return os.environ.get("MS_BOOKING_INTERNO", "").strip() or _organizer()


def impegni(dal: datetime, al: datetime) -> list[tuple[datetime, datetime]]:
    """Gli intervalli occupati sul calendario di chi parla, in UTC.

    Usa getSchedule con availabilityView da 30 minuti — la stessa
    granularita' degli slot: una chiamata copre un'intera giornata.
    Busy e out-of-office bloccano; tentative no (ci finiscono promemoria
    mobili, non impegni veri). Il calendario letto e' MS_BOOKING_INTERNO
    (Giuseppe), non hello@: la casella pubblica e' sempre libera per
    costruzione, gli impegni veri stanno altrove.
    """
    chi = _interno()
    r = _graph("POST", f"/users/{chi}/calendar/getSchedule", json={
        "schedules": [chi],
        "startTime": {"dateTime": dal.astimezone(timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%S"),
                      "timeZone": "UTC"},
        "endTime": {"dateTime": al.astimezone(timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%S"),
                    "timeZone": "UTC"},
        "availabilityViewInterval": 30,
    })
    voci = r.json().get("value") or []
    if not voci:
        return []
    vista = voci[0].get("availabilityView") or ""
    out = []
    for i, c in enumerate(vista):
        if c in ("2", "3"):
            inizio = dal + timedelta(minutes=30 * i)
            out.append((inizio, inizio + timedelta(minutes=30)))
    return out


def crea_evento_teams(nome: str, email: str, azienda: str,
                      inizio: datetime, note: str) -> tuple[str, str]:
    """Crea l'evento con meeting Teams sul calendario pubblico (hello@).
    Outlook spedisce l'invito da solo, al lead e a Giuseppe
    (MS_BOOKING_INTERNO): la call la fa lui, la faccia e' della casella.
    Ritorna (event_id, join_url)."""
    org = _organizer()
    fine = inizio + timedelta(minutes=30)
    chi = azienda or nome
    corpo = (f"Booking from nivult.com/booking<br>"
             f"<b>{nome}</b>{' (' + azienda + ')' if azienda else ''} &lt;{email}&gt;"
             + (f"<br><br>{note}" if note else ""))
    invitati = [{"emailAddress": {"address": email, "name": nome},
                 "type": "required"}]
    interno = os.environ.get("MS_BOOKING_INTERNO", "").strip()
    if interno and interno.lower() != org.lower():
        invitati.append({"emailAddress": {"address": interno},
                         "type": "required"})
    r = _graph("POST", f"/users/{org}/events", json={
        "subject": f"Nivult intro call: {chi}",
        "body": {"contentType": "HTML", "content": corpo},
        "start": {"dateTime": inizio.astimezone(timezone.utc)
                                   .strftime("%Y-%m-%dT%H:%M:%S"),
                  "timeZone": "UTC"},
        "end": {"dateTime": fine.astimezone(timezone.utc)
                               .strftime("%Y-%m-%dT%H:%M:%S"),
                "timeZone": "UTC"},
        "attendees": invitati,
        "isOnlineMeeting": True,
        "onlineMeetingProvider": "teamsForBusiness",
        "responseRequested": True,
    })
    d = r.json()
    join = ((d.get("onlineMeeting") or {}).get("joinUrl")) or ""
    return d["id"], join


def cancella_evento(event_id: str) -> None:
    """Annulla l'evento: il lead riceve la cancellazione da Outlook.
    Se l'evento e' gia' sparito a mano dal calendario, non e' un errore."""
    org = _env("MS_BOOKING_ORGANIZER")
    try:
        _graph("DELETE", f"/users/{org}/events/{event_id}")
    except RuntimeError as e:
        if "(404)" not in str(e):
            raise
