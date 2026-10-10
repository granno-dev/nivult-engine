"""La pagina pubblica delle prenotazioni: nivult.com/booking.

Nata il 10/10/2026. Il lead sceglie uno slot da 30 minuti (lun-ven,
08:00-24:00 ora di Roma), lascia email e volendo il telefono, e riceve
una mail con un bottone: SOLO al click l'evento Teams nasce sul
calendario di Giuseppe via Microsoft Graph (msgraph.py). Il calendario
resta pulito per costruzione, non per fiducia nell'antispam.

Anti-abuso: honeypot, max 3 prenotazioni in attesa per email, max 10 al
giorno per IP. Il POST risponde sempre 200: non rivela se un indirizzo
e' gia' nel sistema.

Le mail partono da delivery.email.invia_generica (SMTP_*), i WhatsApp
dal numero wuzapi SOLO se il lead ha lasciato il telefono. Le funzioni
di invio sono a livello modulo perche' le riusa scripts/booking_reminder.py.
"""
from __future__ import annotations

import ipaddress
import os
import re
import secrets
from datetime import datetime, time as dt_time, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field

from nivult import msgraph
from nivult.config import database_url, load_dotenv
from nivult.delivery import email as email_mod
from nivult.delivery import whatsapp as wa_mod

# Qui si leggono SITE_URL e simili a import-time: il .env va caricato PRIMA,
# perche' in app.py load_dotenv() corre dopo gli import dei moduli.
load_dotenv()

ROMA = ZoneInfo("Europe/Rome")
SLOT_MIN = 30
ORA_INIZIO, ORA_FINE = 8, 24          # 08:00-24:00 Roma, ultimo slot 23:30
ORIZZONTE_GG = 30
PREAVVISO_H = 3
PENDING_BLOCCA_H = 24                 # una pending piu' vecchia non tiene lo slot

SITO = os.environ.get("SITE_URL", "https://nivult.com").rstrip("/")
LOGO_URL = f"{SITO}/brand/nivult-email.png"


class Prenotazione(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    email: EmailStr
    telefono: str | None = Field(default=None, max_length=24)
    azienda: str | None = Field(default=None, max_length=160)
    note: str | None = Field(default=None, max_length=2000)
    starts_at: datetime
    sito: str = ""                      # honeypot: i bot lo compilano


def _telefono_e164(t: str | None) -> str | None:
    """Solo cifre, 8-15: senza questo il messaggio WhatsApp non parte."""
    if not t:
        return None
    cifre = re.sub(r"\D", "", t)
    return cifre if 8 <= len(cifre) <= 15 else None


def _quando(starts_at: datetime) -> str:
    """«Monday 12 October, 14:30 (Rome time)» — la forma nelle mail."""
    s = starts_at.astimezone(ROMA)
    return s.strftime("%A %d %B, %H:%M") + " (Rome time)"


def _email_html(sopra: str, bottone: tuple[str, str] | None,
                piede: str) -> str:
    """Scheletro comune delle mail booking: logo, corpo, un bottone.

    Tabelle e stili inline: Outlook non legge i CSS moderni. Il logo e'
    il PNG esportato da brand/nivult.svg — l'SVG non lo renderizza
    quasi nessun client di posta.
    """
    b = ""
    if bottone:
        testo, url = bottone
        b = (f'<tr><td style="padding:28px 40px 8px" align="center">'
             f'<a href="{url}" style="display:inline-block;background:#2f5de0;'
             f'color:#ffffff;text-decoration:none;font-weight:700;font-size:15px;'
             f'padding:14px 34px;border-radius:10px">{testo}</a></td></tr>')
    return (
        '<!doctype html><html><body style="margin:0;padding:0;background:#f5f4ef">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f5f4ef;padding:32px 12px"><tr><td align="center">'
        '<table role="presentation" width="560" cellpadding="0" cellspacing="0" '
        'style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;'
        'border:1px solid #e7e5df">'
        '<tr><td style="padding:28px 40px 0">'
        f'<img src="{LOGO_URL}" width="140" alt="Nivult" '
        'style="display:block;width:140px;height:auto"></td></tr>'
        f'<tr><td style="padding:24px 40px 8px;font-family:Inter,Helvetica,Arial,'
        f'sans-serif;font-size:15px;line-height:1.65;color:#1e293b">{sopra}</td></tr>'
        f'{b}'
        f'<tr><td style="padding:24px 40px 32px;font-family:Inter,Helvetica,Arial,'
        f'sans-serif;font-size:12.5px;line-height:1.6;color:#94a3b8">{piede}'
        '</td></tr></table>'
        '<div style="font-family:Inter,Helvetica,Arial,sans-serif;font-size:11px;'
        'color:#94a3b8;padding-top:16px">Nivult · the hiring market, measured at '
        'the source · nivult.com</div>'
        '</td></tr></table></body></html>')


def _esc(s: str) -> str:
    return email_mod._esc(s or "")


def mail_conferma(nome: str, email: str, starts_at: datetime, token: str) -> None:
    link = _url_api(f"/booking/conferma/{token}")
    oggetto = f"Confirm your Nivult call: {_quando(starts_at)}"
    corpo = (f"Hi {_esc(nome.split()[0])},<br><br>"
             f"you asked for 30 minutes with us on <b>{_quando(starts_at)}</b>. "
             "One click to confirm and the calendar invite with the Teams link "
             "follows immediately.")
    piede = ("Not you, or changed your mind before confirming? Just ignore "
             "this email: nothing is booked until the button above is clicked.")
    testo = (f"Hi {nome.split()[0]}, confirm your Nivult call on "
             f"{_quando(starts_at)}: {link}\n\nNot you? Ignore this email.")
    email_mod.invia_generica(email, oggetto, testo,
                             _email_html(corpo, ("Confirm the call", link), piede))


def mail_reminder(nome: str, email: str, starts_at: datetime, join_url: str,
                  token: str, ore: int) -> None:
    cancel = _url_api(f"/booking/cancella/{token}")
    quando = _quando(starts_at)
    if ore >= 24:
        oggetto = f"Tomorrow: your Nivult call at {quando}"
        apertura = (f"Hi {_esc(nome.split()[0])}, a quick reminder: we talk "
                    f"<b>{quando}</b>. The Teams link is in the calendar invite"
                    + (f", and here as well:<br><a href=\"{join_url}\">{join_url}</a>"
                       if join_url else "."))
    else:
        oggetto = f"In one hour: your Nivult call"
        apertura = (f"Hi {_esc(nome.split()[0])}, we start in about an hour. "
                    + (f"Join here: <a href=\"{join_url}\">{join_url}</a>"
                       if join_url else
                       "The Teams link is in your calendar invite."))
    piede = (f"Can't make it? <a href=\"{cancel}\">Cancel the call</a>, or "
             f"<a href=\"{SITO}/booking\">pick a new time</a>. "
             "No hard feelings either way.")
    testo = (f"{oggetto}\nJoin: {join_url or '(see the calendar invite)'}\n"
             f"Cancel: {cancel}\nNew time: {SITO}/booking")
    email_mod.invia_generica(email, oggetto, testo,
                             _email_html(apertura, None, piede))


def wa_conferma(nome: str, telefono: str, starts_at: datetime, token: str) -> None:
    link = _url_api(f"/booking/conferma/{token}")
    wa_mod.invia_testo(telefono,
        f"Hi {nome.split()[0]}, it's Nivult. Confirm your call on "
        f"{_quando(starts_at)}: {link}\nIf it wasn't you, just ignore this.")


def wa_reminder(nome: str, telefono: str, starts_at: datetime, join_url: str,
                token: str, ore: int) -> None:
    cancel = _url_api(f"/booking/cancella/{token}")
    quando = _quando(starts_at)
    pre = "tomorrow" if ore >= 24 else "in about one hour"
    testo = f"Nivult: your call is {pre} ({quando})."
    if join_url:
        testo += f"\nJoin: {join_url}"
    testo += f"\nCan't make it? Cancel: {cancel}"
    wa_mod.invia_testo(telefono, testo)


def _slot_giorno(conn, giorno) -> list[datetime]:
    """Gli starts_at liberi di un giorno feriale, in UTC.

    Tre filtri: feriale e dentro l'orizzonte; sopra il preavviso minimo;
    non coperto da Graph (impegni di Giuseppe) ne' da una nostra riga
    pending fresca o confirmed.
    """
    adesso = datetime.now(timezone.utc)
    if giorno.weekday() >= 5:
        return []
    oggi_roma = adesso.astimezone(ROMA).date()
    if giorno < oggi_roma or giorno > oggi_roma + timedelta(days=ORIZZONTE_GG):
        return []

    dal = datetime.combine(giorno, dt_time(ORA_INIZIO), ROMA)
    # le 24:00 sono la mezzanotte del giorno dopo: dt_time(24) non esiste
    al = datetime.combine(giorno + timedelta(days=1), dt_time(0), ROMA)
    impegni = msgraph.impegni(dal, al)

    righe = conn.execute(
        """select starts_at from booking_calls
           where starts_at >= %s and starts_at < %s
             and (status = 'confirmed'
                  or (status = 'pending'
                      and created_at > now() - make_interval(hours => %s)))""",
        (dal, al, PENDING_BLOCCA_H)).fetchall()
    occupati = [(r[0], r[0] + timedelta(minutes=SLOT_MIN)) for r in righe]
    occupati += impegni

    liberi = []
    t = dal
    while t < al:
        fine = t + timedelta(minutes=SLOT_MIN)
        if t >= adesso + timedelta(hours=PREAVVISO_H) and \
                not any(t < b and fine > a for a, b in occupati):
            liberi.append(t)
        t = fine
    return liberi


def _ip(request: Request) -> str | None:
    """Cloudflare sta davanti: l'IP vero e' nel suo header. Se non si
    legge come IP (test locali, proxy strani) resta None: il limite
    per IP degrada in silenzio, quello per email resta."""
    grezzo = request.headers.get("CF-Connecting-IP") \
        or (request.client.host if request.client else "")
    try:
        return str(ipaddress.ip_address(grezzo))
    except ValueError:
        return None


def _url_api(percorso: str) -> str:
    return f"https://api.{SITO.split('://', 1)[1]}{percorso}"


def registra(app) -> None:
    """Attacca le rotte booking all'app: chiamata da create_app()."""

    @app.get("/booking/slots")
    def booking_slots(day: str):
        try:
            giorno = datetime.strptime(day, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(422, "day deve essere YYYY-MM-DD")
        with psycopg.connect(database_url()) as conn:
            return {"timezone": "Europe/Rome",
                    "slots": [s.isoformat() for s in _slot_giorno(conn, giorno)]}

    @app.post("/booking")
    def booking_nuova(p: Prenotazione, request: Request):
        if p.sito:                              # honeypot: bot, si finge ok
            return {"ok": True}
        inizio = p.starts_at.astimezone(timezone.utc)
        telefono = _telefono_e164(p.telefono)
        with psycopg.connect(database_url()) as conn:
            if inizio not in _slot_giorno(conn, inizio.astimezone(ROMA).date()):
                raise HTTPException(409, "slot non piu' disponibile")
            n_email = conn.execute(
                "select count(*) from booking_calls where lower(email) = lower(%s)"
                " and status = 'pending'", (p.email,)).fetchone()[0]
            n_ip = conn.execute(
                "select count(*) from booking_calls where ip = %s::inet"
                " and created_at > now() - interval '1 day'",
                (_ip(request),)).fetchone()[0]
            if n_email >= 3 or n_ip >= 10:
                return {"ok": True}             # silenzioso: non educare l'abuso
            token = secrets.token_urlsafe(32)
            conn.execute(
                """insert into booking_calls
                   (token, nome, email, telefono, azienda, note, starts_at, ip)
                   values (%s, %s, %s, %s, %s, %s, %s, %s::inet)""",
                (token, p.nome.strip(), p.email, telefono,
                 (p.azienda or "").strip() or None,
                 (p.note or "").strip() or None, inizio, _ip(request)))
            conn.commit()
        mail_conferma(p.nome.strip(), p.email, inizio, token)
        if telefono and wa_mod.configurato():
            try:
                wa_conferma(p.nome.strip(), telefono, inizio, token)
            except Exception:
                pass                            # la mail basta da sola
        return {"ok": True}

    @app.get("/booking/conferma/{token}")
    def booking_conferma(token: str):
        with psycopg.connect(database_url()) as conn:
            riga = conn.execute(
                """select id, nome, email, azienda, note, starts_at, status
                   from booking_calls where token = %s""", (token,)).fetchone()
            if not riga:
                return RedirectResponse(f"{SITO}/booking#errore")
            _id, nome, email, azienda, note, inizio, status = riga
            if status == "cancelled":
                return RedirectResponse(f"{SITO}/booking#cancellata")
            if status == "pending":
                # solo il primo click procede: gli altri trovano confirmed
                ok = conn.execute(
                    """update booking_calls set status = 'confirmed'
                       where token = %s and status = 'pending' returning id""",
                    (token,)).fetchone()
                conn.commit()
                if ok:
                    ev, join = msgraph.crea_evento_teams(
                        nome, email, azienda or "", inizio, note or "")
                    conn.execute(
                        """update booking_calls set ms_event_id = %s, join_url = %s
                           where token = %s""", (ev, join, token))
                    conn.commit()
        return RedirectResponse(f"{SITO}/booking#confermata/{token}")

    @app.get("/booking/cancella/{token}")
    def booking_cancella(token: str):
        with psycopg.connect(database_url()) as conn:
            riga = conn.execute(
                "select status, ms_event_id from booking_calls where token = %s",
                (token,)).fetchone()
            if riga and riga[0] != "cancelled":
                if riga[0] == "confirmed" and riga[1]:
                    msgraph.cancella_evento(riga[1])
                conn.execute(
                    "update booking_calls set status = 'cancelled' where token = %s",
                    (token,))
                conn.commit()
        return RedirectResponse(f"{SITO}/booking#cancellata")

    @app.get("/booking/stato/{token}")
    def booking_stato(token: str):
        """La pagina di esito legge i dettagli: ora, link Teams, stato."""
        with psycopg.connect(database_url()) as conn:
            riga = conn.execute(
                """select nome, starts_at, status, join_url
                   from booking_calls where token = %s""", (token,)).fetchone()
        if not riga:
            raise HTTPException(404, "prenotazione non trovata")
        return {"nome": riga[0], "starts_at": riga[1].isoformat(),
                "status": riga[2], "join_url": riga[3] or ""}
