"""Consegna via email: SMTP da variabili d'ambiente, template sobrio.

Le regole di trasparenza del digest sono decisioni di prodotto, non dettagli:

  - l'etichetta della fonte si mostra SEMPRE («candidatura diretta», «via
    France Travail»): è ciò che rende legittimo ammettere anche le agenzie;
  - su datore non dichiarato non si stampa MAI un nome di ripiego: si mostra
    l'etichetta «datore non dichiarato»;
  - lo stipendio si MOSTRA quando c'è, non si filtra mai.

Configurazione: SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM.
Senza SMTP_HOST la consegna vera rifiuta di partire — meglio un errore esplicito
che una email che sembra inviata.

    python -m nivult.delivery.email --anteprima <digest_id>   # solo compilazione
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from nivult.config import load_dotenv

FONTI = {"france_travail": "France Travail",
         "arbetsformedlingen": "Arbetsförmedlingen",
         "bundesagentur": "Bundesagentur"}

MESI = ["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
        "agosto", "settembre", "ottobre", "novembre", "dicembre"]

_UNITA = {"MONTH": "/mese", "YEAR": "/anno", "HOUR": "/ora", "DAY": "/giorno",
          "WEEK": "/settimana"}


def _data(d: datetime) -> str:
    d = d.astimezone(timezone.utc)
    return f"{d.day} {MESI[d.month - 1]}"


def stipendio(salary: dict | None) -> str:
    """Dal MonetaryAmount di schema.org alla riga da mostrare, o vuoto."""
    if not salary:
        return ""
    v = salary.get("value") or {}
    lo, hi = v.get("minValue"), v.get("maxValue")
    unita = _UNITA.get(v.get("unitText"), "")
    valuta = salary.get("currency") or ""
    if lo is not None and hi is not None and lo != hi:
        return f"{lo}–{hi} {valuta}{unita}".strip()
    if lo is not None:
        return f"{lo} {valuta}{unita}".strip()
    if hi is not None:
        return f"{hi} {valuta}{unita}".strip()
    return ""


def etichetta_link(item: dict) -> str:
    if item["link_kind"] == "career_site":
        return "Candidatura diretta"
    if item["link_kind"] == "national_agency":
        return f"Via {FONTI.get(item['source'], item['source'])}"
    return "Apri l'offerta"


def _datore(item: dict) -> str:
    # Su «undisclosed» non si stampa un nome di ripiego: sarebbe una bugia.
    if item.get("organization"):
        return item["organization"]
    return "Datore non dichiarato"


def compila(items: list[dict]) -> tuple[str, str, str]:
    """-> (oggetto, testo, html). Il template è volutamente sobrio."""
    n = len(items)
    oggi = _data(datetime.now(timezone.utc))
    oggetto = f"Nivult — {n} offert{'a' if n == 1 else 'e'} per te ({oggi})"

    righe_testo: list[str] = []
    parti_html: list[str] = []
    for pos, it in enumerate(items, start=1):
        meta = [m for m in [
            ", ".join(it.get("cities") or []) or None,
            stipendio(it.get("salary")) or None,
            f"pubblicata il {_data(it['date_posted'])}",
            ("agenzia di selezione" if it.get("employer_kind") == "staffing_agency"
             else None),
        ] if m]
        righe_testo.append(
            f"{pos}. {it['title']} — {_datore(it)}\n"
            f"   {', '.join(meta)}\n"
            f"   Aderenza {it['score']}/100. {it['reason']}\n"
            f"   {etichetta_link(it)}: {it['url']}\n")
        parti_html.append(
            f'<div style="margin:0 0 28px 0;padding:0 0 28px 0;'
            f'border-bottom:1px solid #e5e5e5;">'
            f'<p style="margin:0;font-size:19px;line-height:1.3;">'
            f'<a href="{it["url"]}" style="color:#111;text-decoration:none;">'
            f'{_esc(it["title"])}</a></p>'
            f'<p style="margin:4px 0 0 0;color:#555;font-size:15px;">'
            f'{_esc(_datore(it))}</p>'
            f'<p style="margin:6px 0 0 0;color:#888;font-size:13px;">'
            f'{_esc(", ".join(meta))}</p>'
            f'<p style="margin:10px 0 0 0;font-size:15px;line-height:1.45;">'
            f'<strong style="color:#111;">{it["score"]}/100</strong> — '
            f'{_esc(it["reason"])}</p>'
            f'<p style="margin:10px 0 0 0;font-size:14px;">'
            f'<a href="{it["url"]}" style="color:#0a5a3c;">'
            f'{_esc(etichetta_link(it))} →</a></p></div>')

    testo = (f"Nivult — digest del {oggi}\n\n"
             + "\n".join(righe_testo)
             + "\nRicevi questo digest perché sei iscritto a Nivult.\n"
               "Le preferenze si gestiscono su nivult.com\n")

    html = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;'
        'padding:32px 24px;color:#111;">'
        '<p style="margin:0 0 4px 0;font-size:13px;letter-spacing:0.12em;'
        'color:#888;">N I V U L T</p>'
        f'<p style="margin:0 0 28px 0;font-size:15px;color:#555;">'
        f'Digest del {oggi}</p>'
        + "".join(parti_html)
        + '<p style="margin:28px 0 0 0;font-size:12px;color:#999;">'
          'Ricevi questo digest perché sei iscritto a <a href="https://nivult.com"'
          ' style="color:#999;">Nivult</a>. Le preferenze si gestiscono su nivult.com.'
          '</p></div>')
    return oggetto, testo, html


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def configurato() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def invia(destinatario: str, items: list[dict]) -> str:
    """Spedisce davvero. Ritorna il Message-ID per digests.provider_message_id."""
    if not configurato():
        raise RuntimeError("SMTP non configurato: servono SMTP_HOST e SMTP_FROM")
    oggetto, testo, html = compila(items)
    msg = EmailMessage()
    msg["Subject"] = oggetto
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = destinatario
    msg["Message-ID"] = make_msgid(domain="nivult.com")
    msg.set_content(testo)
    msg.add_alternative(html, subtype="html")

    host = os.environ["SMTP_HOST"]
    porta = int(os.environ.get("SMTP_PORT", "587"))
    utente, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    if porta == 465:
        server = smtplib.SMTP_SSL(host, porta, timeout=30)
    else:
        server = smtplib.SMTP(host, porta, timeout=30)
    try:
        server.ehlo()
        if porta != 465:
            server.starttls()
            server.ehlo()
        if utente:
            server.login(utente, password or "")
        server.send_message(msg)
    finally:
        server.quit()
    return msg["Message-ID"]


def anteprima(destinatario: str, items: list[dict]) -> str:
    """Compila l'email su file, senza inviare: per il dry-run e i test."""
    oggetto, testo, html = compila(items)
    base = os.path.join(tempfile.gettempdir(), f"nivult-digest-{int(datetime.now().timestamp())}")
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(html)
    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(f"A: {destinatario}\nOggetto: {oggetto}\n\n{testo}")
    return base + ".html"


def main(argv: list[str] | None = None) -> int:
    """Compilazione di prova: un digest finto, per vedere il template."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args(argv)
    load_dotenv()
    esempio = [{
        "title": "HR Business Partner", "organization": "Acme SpA",
        "cities": ["Milano"], "salary": {"value": {"minValue": 55000,
                                                   "maxValue": 65000,
                                                   "unitText": "YEAR"},
                                         "currency": "EUR"},
        "date_posted": datetime.now(timezone.utc), "score": 87,
        "reason": "Ruolo generalista coerente con il profilo e le competenze richieste.",
        "url": "https://acme.example/careers/hrbp", "source": "fantastic",
        "link_kind": "career_site", "employer_kind": "direct",
    }, {
        "title": "Chargé de recrutement", "organization": None,
        "cities": ["Paris"], "salary": None,
        "date_posted": datetime.now(timezone.utc), "score": 82,
        "reason": "Settore e livello affini, sede e lingua compatibili.",
        "url": "https://example.fr/offre/123", "source": "france_travail",
        "link_kind": "national_agency", "employer_kind": "undisclosed",
    }]
    percorso = anteprima("destinatario@example.com", esempio)
    print(f"anteprima scritta: {percorso}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
