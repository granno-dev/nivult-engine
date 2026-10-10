"""Outreach B2B: mail personalizzate dalla shortlist, a ritmo umano.

Nasce il 10/10/2026 dalla lista Apollo (1.000 lead) incrociata col nostro
indice: docs/lead-personalizzati.csv contiene i 48 lead di cui leggiamo
gia' la career page ogni mattina — la mail apre col LORO numero vero di
offerte attive, che nessun competitor puo' scrivere.

Due modi:
  --bozze   (default) scrive le mail in docs/outreach-bozze/ per la
            revisione: niente parte finche' Giuseppe non le approva;
  --invia   manda via SMTP (env OUTREACH_SMTP_HOST/USER/PASS/FROM),
            massimo --al-giorno N (default 10) per corsa, con stato
            in docs/outreach-stato.json: mai due volte allo stesso.

MAI dal relay Brevo dei digest: il cold outreach viola le loro policy e
l'account che muore si porta via i digest dei clienti. La casella giusta
e' una mailbox vera (M365 sul dominio, o un dominio gemello scaldato).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import smtplib
import time
from email.message import EmailMessage
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
LISTA = REPO / "docs" / "lead-personalizzati.csv"
BOZZE = REPO / "docs" / "outreach-bozze"
STATO = REPO / "docs" / "outreach-stato.json"

OGGETTO = "your own hiring footprint, measured daily"

CORPO = """Hi {nome},

We read {dominio} every morning as part of the Nivult index — right now
there are {offerte} open roles on your own career system.

I built Nivult because I kept needing this data myself: live job postings
read directly from employer career systems (never job boards), enriched
with measured models, every field stamped with its source. We track
more than 4 million active postings from 70,000+ employers, refreshed
daily, with a delta feed so your copy never goes stale.

If hiring-market data touches anything you do — competitive intel,
sales signals, analytics — I'd love your take. There's a free sample
dataset on GitHub (no signup): github.com/Nivult/ats-job-data-sample

And if it's not for you, just reply "no thanks" and I won't write again.

Giuseppe
Nivult — nivult.com
"""


def carica_stato() -> dict:
    if STATO.exists():
        return json.loads(STATO.read_text())
    return {"inviate": {}}


def salva_stato(s: dict) -> None:
    STATO.write_text(json.dumps(s, indent=1, ensure_ascii=False))


def componi(riga: dict) -> tuple[str, str]:
    nome = riga["nome"].split()[0]
    corpo = CORPO.format(nome=nome, dominio=riga["dominio"],
                         offerte=riga["offerte_attive_nell_indice"])
    return OGGETTO, corpo


def main() -> int:
    ap = argparse.ArgumentParser(prog="nivult outreach")
    ap.add_argument("--invia", action="store_true",
                    help="manda davvero (default: solo bozze)")
    ap.add_argument("--al-giorno", type=int, default=10)
    args = ap.parse_args()

    stato = carica_stato()
    lead = [r for r in csv.DictReader(open(LISTA))
            if r["email"] and r["email"] not in stato["inviate"]]
    print(f"da contattare: {len(lead)}")

    if not args.invia:
        BOZZE.mkdir(exist_ok=True)
        for r in lead:
            oggetto, corpo = componi(r)
            (BOZZE / f"{r['dominio']}.txt").write_text(
                f"To: {r['email']}\nSubject: {oggetto}\n\n{corpo}")
        print(f"bozze scritte in {BOZZE} — rivedile, poi --invia")
        return 0

    host = os.environ.get("OUTREACH_SMTP_HOST", "")
    if not host:
        raise SystemExit("manca OUTREACH_SMTP_HOST/USER/PASS/FROM: "
                         "configura la casella di invio prima")
    mittente = os.environ["OUTREACH_SMTP_FROM"]
    da_mandare = lead[: args.al_giorno]
    with smtplib.SMTP(host, int(os.environ.get("OUTREACH_SMTP_PORT", "587"))) as s:
        s.starttls()
        s.login(os.environ["OUTREACH_SMTP_USER"],
                os.environ["OUTREACH_SMTP_PASS"])
        for r in da_mandare:
            oggetto, corpo = componi(r)
            m = EmailMessage()
            m["From"] = mittente
            m["To"] = r["email"]
            m["Subject"] = oggetto
            m.set_content(corpo)
            s.send_message(m)
            stato["inviate"][r["email"]] = time.strftime("%Y-%m-%d %H:%M")
            salva_stato(stato)          # ogni mail e' registrata subito
            print(f"-> {r['email']} ({r['dominio']})")
            time.sleep(45)              # ritmo umano: mai una raffica
    print(f"inviate oggi: {len(da_mandare)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
