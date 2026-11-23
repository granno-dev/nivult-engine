#!/usr/bin/env python3
"""Verifica locale di CV e configurazione prodotto, senza DB, rete o dati reali.

    python scripts/check_cv.py
"""
from __future__ import annotations

import contextlib
import io
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from docx import Document  # noqa: E402

from nivult import config, cv  # noqa: E402
from nivult import retention_utenti  # noqa: E402
from nivult.delivery import email as email_mod  # noqa: E402
from nivult.delivery import telegram, whatsapp  # noqa: E402
from nivult.delivery.testi import t  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condizione: bool, dettaglio: str = "") -> None:
    if condizione:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}{(' — ' + dettaglio) if dettaglio else ''}")


@contextlib.contextmanager
def ambiente(**valori):
    mancante = object()
    originali = {chiave: os.environ.get(chiave, mancante) for chiave in valori}
    try:
        for chiave, valore in valori.items():
            if valore is None:
                os.environ.pop(chiave, None)
            else:
                os.environ[chiave] = valore
        yield
    finally:
        for chiave, valore in originali.items():
            if valore is mancante:
                os.environ.pop(chiave, None)
            else:
                os.environ[chiave] = valore


def _docx() -> bytes:
    documento = Document()
    documento.add_paragraph(
        "Curriculum vitae sintetico con esperienza professionale e risultati misurabili.")
    tabella = documento.add_table(rows=2, cols=2)
    tabella.cell(0, 0).text = "Competenza"
    tabella.cell(0, 1).text = "Project management"
    tabella.cell(1, 0).text = "Lingua"
    tabella.cell(1, 1).text = "Italiano"
    stream = io.BytesIO()
    documento.save(stream)
    return stream.getvalue()


def _pdf(testo: str) -> bytes:
    testo_pdf = testo.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({testo_pdf}) Tj ET".encode("ascii")
    oggetti = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    ]
    dati = bytearray(b"%PDF-1.4\n")
    offset = [0]
    for numero, oggetto in enumerate(oggetti, start=1):
        offset.append(len(dati))
        dati.extend(f"{numero} 0 obj\n".encode())
        dati.extend(oggetto)
        dati.extend(b"\nendobj\n")
    inizio_xref = len(dati)
    dati.extend(f"xref\n0 {len(oggetti) + 1}\n".encode())
    dati.extend(b"0000000000 65535 f \n")
    for posizione in offset[1:]:
        dati.extend(f"{posizione:010d} 00000 n \n".encode())
    dati.extend(
        f"trailer\n<< /Size {len(oggetti) + 1} /Root 1 0 R >>\n"
        f"startxref\n{inizio_xref}\n%%EOF\n".encode())
    return bytes(dati)


def _item() -> dict:
    return {
        "title": "Project Manager", "organization": "Acme Example",
        "cities": ["Milano"], "salary": None,
        "date_posted": datetime(2026, 1, 2, tzinfo=timezone.utc),
        "score": 87, "reason": "Motivazione sintetica.",
        "url": "https://example.test/jobs/1", "source": "fantastic",
        "link_kind": "career_site", "employer_kind": "direct",
    }


def check_config() -> None:
    print("\nConfigurazione e default")
    chiavi = dict.fromkeys(("PRODUCT_NAME", "SITE_URL", "SUPPORT_EMAIL",
                            "DEFAULT_DELIVERY_CHANNELS", "B2B_ENABLED", "EXPORT_DIR"))
    with ambiente(**chiavi):
        check("product_name default Nivult", config.product_name() == "Nivult")
        check("site_url default Nivult", config.site_url() == "https://nivult.com")
        check("support_email default", config.support_email() == "hello@nivult.com")
        check("canali default email", config.default_delivery_channels() == ["email"])
        check("B2B default attivo", config.b2b_enabled() is True)
        check("export_dir default", config.export_dir() == Path("/opt/nivult/exports"))
        check("testi Nivult invariati", t("en")["ml_oggetto"] == "Your Nivult sign-in link")

    with ambiente(PRODUCT_NAME="Mavult", SITE_URL="https://mavult.com/",
                  SUPPORT_EMAIL="support@mavult.com",
                  DEFAULT_DELIVERY_CHANNELS="app, email, app", B2B_ENABLED="0",
                  EXPORT_DIR="/tmp/mavult-exports"):
        check("product_name Mavult", config.product_name() == "Mavult")
        check("site_url senza slash finale", config.site_url() == "https://mavult.com")
        check("support_email Mavult", config.support_email() == "support@mavult.com")
        check("canali ordinati e deduplicati",
              config.default_delivery_channels() == ["app", "email"])
        check("B2B Mavult disattivo", config.b2b_enabled() is False)
        check("export_dir Mavult", config.export_dir() == Path("/tmp/mavult-exports"))
        check("magic link tradotto e brandizzato",
              t("it")["ml_oggetto"] == "Il tuo accesso a Mavult")

    with ambiente(DEFAULT_DELIVERY_CHANNELS="app,non-esiste"):
        try:
            config.default_delivery_channels()
        except ValueError:
            check("canale sconosciuto rifiutato", True)
        else:
            check("canale sconosciuto rifiutato", False)
    with ambiente(B2B_ENABLED="forse"):
        try:
            config.b2b_enabled()
        except ValueError:
            check("booleano B2B ambiguo rifiutato", True)
        else:
            check("booleano B2B ambiguo rifiutato", False)


def check_parser() -> None:
    print("\nParser e formati CV")
    check("tetto unico 20 MiB", cv.MAX_BYTES == 20 * 1024 * 1024)
    check("città, paese e trasferimento dal CV",
          cv.estrai_localita("Rome, Italy - Open to Relocate\nHR Business Partner"),
          {"location_city": "Rome", "location_country": "IT", "relocation_open": True})
    check("residenza in una frase non include il prefisso",
          cv.estrai_localita("I'm based in Rome, and willing to relocate."),
          {"location_city": "Rome", "location_country": "IT", "relocation_open": True})
    check("località assente resta sconosciuta",
          cv.estrai_localita("HR Business Partner con otto anni di esperienza."),
          {"location_city": None, "location_country": None, "relocation_open": None})

    testo_docx = cv.estrai_testo("cv.bin", cv.DOCX_MIME, _docx())
    check("DOCX reale riconosciuto dal MIME", "Curriculum vitae sintetico" in testo_docx)
    check("DOCX include paragrafi e tabelle",
          "Project management" in testo_docx and "Italiano" in testo_docx)

    frase = "Synthetic CV with enough professional experience for PDF parsing."
    testo_pdf = cv.estrai_testo("cv.pdf", "application/pdf", _pdf(frase))
    check("PDF reale estratto", frase in testo_pdf)

    testo_txt = cv.estrai_testo(
        "cv.txt", "text/plain; charset=utf-8",
        "Cafe\u0301\x00 — esperienza professionale e competenze".encode())
    check("TXT normalizzato NFC e senza NUL", testo_txt.startswith("Café —") and "\x00" not in testo_txt)
    normalizzato = cv._normalizza("Cafe\u0301\ud800\udfff\x00 testo")
    check("surrogati rimossi", normalizzato == "Café testo")

    for nome, mime in (("cv.doc", "application/msword"),
                       ("cv.doc", cv.DOCX_MIME)):
        try:
            cv.estrai_testo(nome, mime, b"contenuto legacy sintetico lungo almeno trenta caratteri")
        except ValueError as exc:
            check(f"legacy .doc rifiutato ({mime})", ".doc" in str(exc))
        else:
            check(f"legacy .doc rifiutato ({mime})", False)

    for nome, dati in (("cv.pdf", b"PDF rotto con marcatore privato sintetico"),
                       ("cv.docx", b"PK DOCX rotto con marcatore privato sintetico")):
        registri: list[str] = []

        class Raccogli(logging.Handler):
            def emit(self, record):
                registri.append(self.format(record))

        radice = logging.getLogger()
        handler = Raccogli()
        livello = radice.level
        radice.addHandler(handler)
        radice.setLevel(logging.WARNING)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    cv.estrai_testo(nome, "", dati)
                except ValueError as exc:
                    messaggio = str(exc)
                else:
                    messaggio = ""
        finally:
            radice.removeHandler(handler)
            radice.setLevel(livello)
        check(f"{nome} malformato diventa ValueError sicura",
              bool(messaggio) and "marcatore privato" not in messaggio)
        check(f"{nome} malformato non scrive contenuto nei log",
              not any("marcatore privato" in riga or "PDF rotto" in riga or "DOCX rotto" in riga
                      for riga in registri))

    try:
        cv.estrai_testo("cv.txt", "text/plain", b"troppo corto")
    except ValueError:
        check("testo insufficiente rifiutato", True)
    else:
        check("testo insufficiente rifiutato", False)

    check("un profilo senza contenuto professionale viene rifiutato",
          not cv.contiene_dati_professionali({
              "families": [], "skills": [], "years_experience": None,
              "raw_extraction": {"roles": [], "certifications": [], "education": []},
          }))
    check("ruoli, competenze o formazione rendono il documento professionale",
          all(cv.contiene_dati_professionali(profilo) for profilo in (
              {"families": ["Human Resources"]},
              {"skills": ["Workday"]},
              {"years_experience": 5},
              {"raw_extraction": {"roles": [{"title": "HR Specialist"}]}},
              {"raw_extraction": {"certifications": ["Lean Six Sigma"]}},
              {"raw_extraction": {"education": [{"title": "Master"}]}},
          )))

    al_tetto = b"x" * cv.MAX_BYTES
    check("20 MiB esatti accettati", len(cv.estrai_testo("cv.txt", "text/plain", al_tetto))
          == cv.MAX_CARATTERI)
    try:
        cv.estrai_testo("cv.txt", "text/plain", al_tetto + b"x")
    except ValueError as exc:
        check("20 MiB + 1 rifiutato", "20" in str(exc))
    else:
        check("20 MiB + 1 rifiutato", False)


class SMTPFinto:
    ultimo = None

    def __init__(self, *args, **kwargs):
        self.messaggio = None
        self.chiuso = False
        SMTPFinto.ultimo = self

    def ehlo(self):
        return None

    def starttls(self):
        return None

    def login(self, *args):
        return None

    def send_message(self, messaggio):
        self.messaggio = messaggio

    def quit(self):
        self.chiuso = True


def check_delivery() -> None:
    print("\nBrand, link e header di consegna")
    with ambiente(PRODUCT_NAME="Mavult", SITE_URL="https://mavult.com",
                  SUPPORT_EMAIL="support@mavult.com", TELEGRAM_BOT_USERNAME=None,
                  MS_MAIL_FROM=None, SMTP_HOST="smtp.invalid", SMTP_PORT="587",
                  SMTP_FROM="digest@mavult.com", SMTP_USER=None, SMTP_PASSWORD=None):
        oggetto, testo, html = email_mod.compila([_item()], "it")
        corpo_email = oggetto + testo + html
        corpo_telegram = "\n".join(telegram.compila([_item()], "it"))
        corpo_whatsapp = "\n".join(whatsapp._compila([_item()], "it", None))
        for canale, corpo in (("email", corpo_email), ("Telegram", corpo_telegram),
                              ("WhatsApp", corpo_whatsapp)):
            check(f"digest {canale} Mavult senza rimandi Nivult",
                  "Mavult" in corpo and "Nivult" not in corpo and "nivult.com" not in corpo)
        check("footer email usa il sito Mavult", "mavult.com" in corpo_email)
        check("deep link Telegram brandizzato", "MavultBot" in telegram.link_collegamento("x" * 24))
        check("deep link WhatsApp brandizzato",
              "MAVULT" in whatsapp.link_collegamento("x" * 24)
              and "NIVULT" not in whatsapp.link_collegamento("x" * 24))

        originale = email_mod.smtplib.SMTP
        email_mod.smtplib.SMTP = SMTPFinto
        try:
            email_mod.invia_generica("utente@example.test", "Prova", "Testo", "<p>Testo</p>")
        finally:
            email_mod.smtplib.SMTP = originale
        messaggio = SMTPFinto.ultimo.messaggio
        check("Message-ID dal dominio del prodotto", messaggio["Message-ID"].endswith("@mavult.com>"))
        check("unsubscribe dal supporto del prodotto",
              messaggio["List-Unsubscribe"] == "<mailto:support@mavult.com?subject=unsubscribe>")
        check("SMTP simulato chiuso senza rete", SMTPFinto.ultimo.chiuso)

        ricevuta = {}
        originale_invia = email_mod.invia_generica
        email_mod.invia_generica = lambda destinatario, oggetto, testo, html: ricevuta.update(
            destinatario=destinatario, oggetto=oggetto, testo=testo, html=html)
        try:
            retention_utenti._avviso_email(
                retention_utenti.Utente("id", "utente@example.test", "en", "inattivita"))
        finally:
            email_mod.invia_generica = originale_invia
        check("retention brand, login e supporto Mavult",
              "Mavult" in ricevuta["oggetto"]
              and "https://mavult.com/login" in ricevuta["testo"]
              and "support@mavult.com" in ricevuta["testo"]
              and "nivult.com" not in ricevuta["testo"])


def main() -> int:
    print("verifica CV/config — fixture sintetiche, nessun DB o invio")
    check_config()
    check_parser()
    check_delivery()
    print(f"\n{len(PASSED)} controlli ok, {len(FAILED)} falliti")
    if FAILED:
        print("Falliti: " + ", ".join(FAILED))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
