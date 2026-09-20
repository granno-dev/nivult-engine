"""Quante AZIENDE VERE sta scartando il filtro?

`x\\.com` (Twitter) blocca `besix.com`, `netflix.com`, `xerox.com`: la regex non e'
ancorata a un confine di dominio, quindi prende qualunque cosa finisca cosi'.
Un filtro troppo largo costa copertura in silenzio — peggio di uno che lascia
passare qualche bacheca, perche' quella almeno si vede.

La prova si fa sui domini che abbiamo GIA' e che sappiamo giusti (passati dal
giudice): ogni scarto li' dentro e' un falso scarto.
"""
import os
import importlib.util
import re

import psycopg

spec = importlib.util.spec_from_file_location("cd", "/opt/nivult/caccia_domini.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

ATS_VERI = re.compile(r"(zohorecruit|rippling|ashbyhq|ashbyprd|bamboohr|lever\.co|workable|"
                      r"myworkday|greenhouse|smartrecruiters|personio|teamtailor|recruitee|"
                      r"icims|jobvite|taleo|successfactors|careerpuck|cloud\.sap|trakstar|"
                      r"pinpointhq|jobsoid|catsone|applytojob|breezy)", re.I)

with psycopg.connect(os.environ["ATS_DATABASE_URL"]) as c:
    domini = [r[0] for r in c.execute("""
        SELECT DISTINCT site_domain FROM ats_companies
         WHERE site_domain IS NOT NULL AND site_domain_livello = 1
         LIMIT 4000""").fetchall()]

# si escludono quelli che SONO davvero bacheche: quelli lo scarto se lo meritano
buoni = [d for d in domini if not ATS_VERI.search(d)]
scartati = [d for d in buoni if m.NON_AZIENDA.search(d)]

print(f"domini gia' provati e non-bacheca: {len(buoni):,}")
print(f"che il filtro attuale scarterebbe: {len(scartati):,}  ({100*len(scartati)/max(len(buoni),1):.1f}%)\n")

alt = [a for a in re.split(r"\|", m.NON_AZIENDA.pattern.replace("(?:", "").replace(")", "").replace("(", "")) if a]
colpe = {}
for d in scartati:
    for a in alt:
        if re.search(a, d, re.I):
            colpe.setdefault(a, []).append(d)

print("le regole che scartano di piu', e cosa scartano:")
for a, ds in sorted(colpe.items(), key=lambda x: -len(x[1]))[:12]:
    print(f"  {a:<24}{len(ds):>5}   {', '.join(ds[:4])}")
