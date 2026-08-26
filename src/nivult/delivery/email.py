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
from nivult.delivery.testi import mesi, t

FONTI = {"france_travail": "France Travail",
         "arbetsformedlingen": "Arbetsförmedlingen",
         "bundesagentur": "Bundesagentur"}


def _data(d: datetime, locale: str) -> str:
    d = d.astimezone(timezone.utc)
    # Il tedesco scrive il giorno come ordinale: «26. August», col punto.
    punto = "." if locale == "de" else ""
    return f"{d.day}{punto} {mesi(locale)[d.month - 1]}"


def stipendio(salary: dict | None, locale: str = "en") -> str:
    """Dal MonetaryAmount di schema.org alla riga da mostrare, o vuoto."""
    if not salary:
        return ""
    v = salary.get("value") or {}
    lo, hi = v.get("minValue"), v.get("maxValue")
    _UNITA = {"MONTH": t(locale)["unita_mese"], "YEAR": t(locale)["unita_anno"],
              "HOUR": t(locale)["unita_ora"], "DAY": t(locale)["unita_giorno"],
              "WEEK": t(locale)["unita_settimana"]}
    unita = _UNITA.get(v.get("unitText"), "")
    valuta = salary.get("currency") or ""
    if lo is not None and hi is not None and lo != hi:
        return f"{lo}–{hi} {valuta}{unita}".strip()
    if lo is not None:
        return f"{lo} {valuta}{unita}".strip()
    if hi is not None:
        return f"{hi} {valuta}{unita}".strip()
    return ""


def etichetta_link(item: dict, locale: str = "en") -> str:
    if item["link_kind"] == "career_site":
        return t(locale)["candidatura_diretta"]
    if item["link_kind"] == "national_agency":
        return t(locale)["via"].format(fonte=FONTI.get(item["source"], item["source"]))
    return t(locale)["apri"]


def _datore(item: dict, locale: str = "en") -> str:
    # Su «undisclosed» non si stampa un nome di ripiego: sarebbe una bugia.
    if item.get("organization"):
        return item["organization"]
    return t(locale)["datore_non_dichiarato"]


def compila(items: list[dict], locale: str = "en") -> tuple[str, str, str]:
    """-> (oggetto, testo, html), nella lingua dell'utente.

    Il template viene da `testi.py`; le motivazioni dentro gli item arrivano
    già nella lingua giusta, generate così da GLM — qui non si traduce nulla.
    """
    n = len(items)
    x = t(locale)
    oggi = _data(datetime.now(timezone.utc), locale)
    chiave_plurale = "oggetto_molte"
    if n == 1:
        chiave_plurale = "oggetto_uno"
    elif locale == "pl" and n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        # Il polacco ha TRE plurali: 1 oferta, 2-4 oferty, 5+ ofert — e la
        # regola si ripete per decina (22 oferty, ma 12 ofert).
        chiave_plurale = "oggetto_poche"
    oggetto = x[chiave_plurale].format(n=n, data=oggi)

    righe_testo: list[str] = []
    parti_html: list[str] = []
    for pos, it in enumerate(items, start=1):
        meta = [m for m in [
            ", ".join(it.get("cities") or []) or None,
            stipendio(it.get("salary"), locale) or None,
            x["pubblicata"].format(data=_data(it["date_posted"], locale)),
            (x["agenzia"] if it.get("employer_kind") == "staffing_agency"
             else None),
        ] if m]
        righe_testo.append(
            f"{pos}. {it['title']} — {_datore(it, locale)}\n"
            f"   {', '.join(meta)}\n"
            f"   {x['aderenza']} {it['score']}/100. {it['reason']}\n"
            f"   {etichetta_link(it, locale)}: {it['url']}\n")
        parti_html.append(
            f'<div style="margin:0 0 28px 0;padding:0 0 28px 0;'
            f'border-bottom:1px solid #e5e5e5;">'
            f'<p style="margin:0;font-size:19px;line-height:1.3;">'
            f'<a href="{_esc(it["url"])}" style="color:#111;text-decoration:none;">'
            f'{_esc(it["title"])}</a></p>'
            f'<p style="margin:4px 0 0 0;color:#555;font-size:15px;">'
            f'{_esc(_datore(it, locale))}</p>'
            f'<p style="margin:6px 0 0 0;color:#888;font-size:13px;">'
            f'{_esc(", ".join(meta))}</p>'
            f'<p style="margin:10px 0 0 0;font-size:15px;line-height:1.45;">'
            f'<strong style="color:#111;">{it["score"]}/100</strong> — '
            f'{_esc(it["reason"])}</p>'
            f'<p style="margin:10px 0 0 0;font-size:14px;">'
            f'<a href="{_esc(it["url"])}" style="color:#0a5a3c;">'
            f'{_esc(etichetta_link(it, locale))} →</a></p></div>')

    testo = (f"Nivult — {x['digest_del'].format(data=oggi)}\n\n"
             + "\n".join(righe_testo)
             + "\n" + x["piede_testo"] + "\n")

    html = (
        '<div style="font-family:Georgia,serif;max-width:560px;margin:0 auto;'
        'padding:32px 24px;color:#111;">'
        '<p style="margin:0 0 4px 0;font-size:13px;letter-spacing:0.12em;'
        'color:#888;">N I V U L T</p>'
        f'<p style="margin:0 0 28px 0;font-size:15px;color:#555;">'
        f'{_esc(x["digest_del"].format(data=oggi))}</p>'
        + "".join(parti_html)
        + f'<p style="margin:28px 0 0 0;font-size:12px;color:#999;">'
          f'{_esc(x["piede"])}</p></div>')
    return oggetto, testo, html


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def configurato() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def invia(destinatario: str, items: list[dict], locale: str = "en") -> str:
    """Spedisce un digest. Ritorna il Message-ID per digests.provider_message_id."""
    oggetto, testo, html = compila(items, locale)
    return invia_generica(destinatario, oggetto, testo, html)


def invia_generica(destinatario: str, oggetto: str, testo: str, html: str) -> str:
    """Spedisce una qualsiasi email transazionale: digest, magic link, allarmi."""
    if not configurato():
        raise RuntimeError("SMTP non configurato: servono SMTP_HOST e SMTP_FROM")
    msg = EmailMessage()
    msg["Subject"] = oggetto
    msg["From"] = os.environ["SMTP_FROM"]
    msg["To"] = destinatario
    msg["Message-ID"] = make_msgid(domain="nivult.com")
    # Gmail e Yahoo lo chiedono ai sender ricorrenti, e senza si finisce in
    # spam proprio col prodotto principale. Il mailto basta al requisito
    # finché non esiste una rotta di disiscrizione a un clic.
    msg["List-Unsubscribe"] = "<mailto:hello@nivult.com?subject=unsubscribe>"
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


def anteprima(destinatario: str, items: list[dict], locale: str = "en") -> str:
    """Compila l'email su file, senza inviare: per il dry-run e i test."""
    oggetto, testo, html = compila(items, locale)
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
