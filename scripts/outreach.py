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

# Corpi per segmento (colonna "segmento" del CSV; assente = A_nell_indice).
# Le liste nuove da Apollo: docs/lead-apollo-2026-10-10.csv con
#   --lista docs/lead-apollo-2026-10-10.csv --bozze-dir docs/outreach-bozze-apollo
# Il segmento D_altro non riceve bozze: priorita' troppo bassa.

OGGETTO_A = "your own hiring footprint, measured daily"

CORPO_A = """Hi {nome},

We read {dominio} every morning as part of the Nivult index — right now
there are {offerte} on your own career system.

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

OGGETTO_B = "who's hiring in your market, measured daily"

CORPO_B = """Hi {nome},

Quick question: when you advise clients on hiring, where do you get
live market data?

I built Nivult: we read job postings directly from employer career
systems (never job boards) — more than 4 million active postings from
70,000+ employers, refreshed daily, every field stamped with its
source. Recruiters use it to see which companies are hiring, for which
roles, and where — before it shows up on any job board.

Happy to send a free sample cut on your market (no signup, no call
needed): github.com/Nivult/ats-job-data-sample

And if it's not for you, just reply "no thanks" and I won't write again.

Giuseppe
Nivult — nivult.com
"""

OGGETTO_C = "the hiring market, measured daily"

CORPO_C = """Hi {nome},

I built Nivult, a live index of the hiring market: more than 4 million
active job postings read daily from 70,000+ employer career systems
(never job boards), enriched with measured models, every field stamped
with its source.

Teams use it for competitive intel, sales signals, and market
analytics — with a delta feed so your copy never goes stale.

There's a free sample dataset on GitHub (no signup):
github.com/Nivult/ats-job-data-sample

If it's not for you, just reply "no thanks" and I won't write again.

Giuseppe
Nivult — nivult.com
"""

TEMPLATE = {
    "A_nell_indice": (OGGETTO_A, CORPO_A),
    "B_hr_staffing": (OGGETTO_B, CORPO_B),
    "C_finanziati": (OGGETTO_C, CORPO_C),
}


def carica_stato() -> dict:
    if STATO.exists():
        return json.loads(STATO.read_text())
    return {"inviate": {}}


def salva_stato(s: dict) -> None:
    STATO.write_text(json.dumps(s, indent=1, ensure_ascii=False))


def componi(riga: dict) -> tuple[str, str] | None:
    oggetto, corpo_t = TEMPLATE.get(riga.get("segmento") or "A_nell_indice",
                                    (None, None))
    if corpo_t is None:
        return None
    nome = riga["nome"].split()[0]
    n = int(riga["offerte_attive_nell_indice"] or 0)
    corpo = corpo_t.format(nome=nome, dominio=riga["dominio"],
                           offerte=f"{n} open role" + ("s" if n != 1 else ""))
    return oggetto, corpo


def main() -> int:
    ap = argparse.ArgumentParser(prog="nivult outreach")
    ap.add_argument("--invia", action="store_true",
                    help="manda davvero (default: solo bozze)")
    ap.add_argument("--al-giorno", type=int, default=10)
    ap.add_argument("--lista", type=Path, default=LISTA,
                    help="CSV dei lead (default: docs/lead-personalizzati.csv)")
    ap.add_argument("--bozze-dir", type=Path, default=BOZZE,
                    help="cartella delle bozze (default: docs/outreach-bozze)")
    args = ap.parse_args()

    stato = carica_stato()
    lead = []
    for r in csv.DictReader(open(args.lista)):
        if not r["email"] or r["email"] in stato["inviate"]:
            continue
        if componi(r) is None:
            continue                    # segmento senza template: saltato
        lead.append(r)
    print(f"da contattare: {len(lead)}")

    if not args.invia:
        args.bozze_dir.mkdir(exist_ok=True)
        for r in lead:
            oggetto, corpo = componi(r)
            (args.bozze_dir / f"{r['dominio']}.txt").write_text(
                f"To: {r['email']}\nSubject: {oggetto}\n\n{corpo}")
        print(f"bozze scritte in {args.bozze_dir} — rivedile, poi --invia")
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
