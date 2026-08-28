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
        # La riga datore · citta' sta sotto il titolo; il resto dei meta
        # (stipendio, data, agenzia) in una riga fine sotto.
        citta = ", ".join(it.get("cities") or [])
        sotto_titolo = " · ".join(z for z in [_datore(it, locale), citta] if z)
        meta_fini = [m for m in meta if m and m != citta]
        parti_html.append(
            f'<tr><td style="padding:6px 0;">'
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0" style="background:#ffffff;border:1px solid '
            f'#e5e7eb;border-radius:14px;">'
            f'<tr><td style="padding:20px 22px;">'
            # titolo a sinistra, badge del punteggio a destra
            f'<table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0"><tr>'
            f'<td valign="top" style="padding-right:12px;">'
            f'<a href="{_esc(it["url"])}" style="color:#0f172a;'
            f'text-decoration:none;font-size:17px;font-weight:600;'
            f'line-height:1.35;">{_esc(it["title"])}</a>'
            f'<div style="margin-top:3px;color:#4b5563;font-size:14px;">'
            f'{_esc(sotto_titolo)}</div></td>'
            f'<td valign="top" align="right" width="48">'
            f'<span style="display:inline-block;background:#3355ff;'
            f'color:#ffffff;font-size:14px;font-weight:700;'
            f'padding:6px 10px;border-radius:9px;">{it["score"]}</span>'
            f'</td></tr></table>'
            + (f'<div style="margin-top:10px;color:#9ca3af;font-size:12.5px;">'
               f'{_esc(" · ".join(meta_fini))}</div>' if meta_fini else "")
            + f'<div style="margin-top:12px;color:#374151;font-size:14px;'
              f'line-height:1.55;">{_esc(it["reason"])}</div>'
            # bottone a prova di Outlook: una cella colorata, non un CSS
            f'<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="margin-top:14px;"><tr>'
            f'<td style="background:#3355ff;border-radius:9px;">'
            f'<a href="{_esc(it["url"])}" style="display:inline-block;'
            f'padding:10px 18px;color:#ffffff;font-size:14px;'
            f'font-weight:600;text-decoration:none;">'
            f'{_esc(etichetta_link(it, locale))} &rarr;</a>'
            f'</td></tr></table>'
            f'</td></tr></table></td></tr>')

    testo = (f"Nivult — {x['digest_del'].format(data=oggi)}\n\n"
             + "\n".join(righe_testo)
             + "\n" + x["piede_testo"] + "\n")

    # Tabelle e stili inline: e' l'unico dialetto che Outlook capisce.
    # Palette e voce del sito: canvas fuori, card bianche dentro, accent
    # solo dove si agisce (badge e bottone).
    carattere = ("-apple-system,'Segoe UI',Roboto,Helvetica,Arial,"
                 "sans-serif")
    html = (
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0" style="background:#f4f6f5;">'
        f'<tr><td align="center" style="padding:28px 12px;">'
        f'<table role="presentation" width="600" cellpadding="0" '
        f'cellspacing="0" style="max-width:600px;width:100%;'
        f'font-family:{carattere};">'
        # testata: il marchio a sinistra, la data a destra
        f'<tr><td style="padding:0 6px 14px;">'
        f'<table role="presentation" width="100%" cellpadding="0" '
        f'cellspacing="0"><tr>'
        f'<td style="font-size:19px;font-weight:700;color:#0f172a;">'
        f'Nivult<span style="color:#3355ff;">.</span></td>'
        f'<td align="right" style="font-size:13px;color:#6b7280;">'
        f'{_esc(x["digest_del"].format(data=oggi))}</td>'
        f'</tr></table></td></tr>'
        + "".join(parti_html)
        + f'<tr><td style="padding:18px 6px 0;color:#9ca3af;'
          f'font-size:12px;line-height:1.55;">{_esc(x["piede"])}'
          f'</td></tr>'
          f'</table></td></tr></table>')
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
