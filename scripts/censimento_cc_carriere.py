#!/usr/bin/env python3
"""Censimento europeo dalle pagine carriere VERE di Common Crawl.

Il censimento GLEIF inventava i domini dal nome legale (94% morti). Qui
il dominio arriva da una pagina carriere davvero esistita: l'indice
colonnare di Common Crawl (parquet, un crawl = 300 file) letto con
DuckDB in predicate pushdown — si scaricano solo i row group dei TLD
europei — cercando nei path i vocabolari delle nove lingue
(`lavora-con-noi`, `carriere`, `recrutement`, `karriere`,
`trabaja-con-nosotros`, `werken-bij`, `kariera`, `ledige-stillinger`…).

Due passi, entrambi sul N5:

    # 1. la scansione (≈20 min, scrive /opt/nivult/cc_carriere.csv)
    python /opt/nivult/cc_censimento.py CC-MAIN-2026-34
    # 2. l'aggregazione in company_domains (questo script)
    ATS_DATABASE_URL=... python scripts/censimento_cc_carriere.py /opt/nivult/cc_carriere.csv --dry-run

Ogni dominio entra con `source='cc_carriere'`, il paese dal TLD (uk→GB),
la `careers_url` migliore (la parola piu' specifica vince: «lavora-con-
noi» batte «/jobs») e `status='pending'`: il detector lo visita, e
grazie al paese finisce in testa alla coda europea. Chi era gia' censito
ma senza paese (grafo_cc, http_archive) riceve paese e careers_url.
"""
from __future__ import annotations

import argparse
import collections
import csv
import os
import re
import sys

import psycopg

PAESE_TLD = {"uk": "GB"}
# parole in ordine di specificita' (prima = meglio): la careers_url scelta
SPECIFICHE = ["lavora-con-noi", "lavora_con_noi", "lavoraconnoi", "posizioni-aperte",
              "nous-rejoindre", "recrutement", "offres-d-emploi", "rejoignez",
              "stellenangebote", "jobs-und-karriere", "karriere",
              "trabaja-con-nosotros", "ofertas-de-empleo", "werken-bij", "vacatures",
              "ledige-stillinger", "lediga-jobb", "avoimet-tyopaikat", "kariera",
              "/carriere", "/carrieres", "/careers", "/career/", "/vacancies",
              "/candidati", "/empleo", "/emplois", "/stellen", "/praca", "/jobb",
              "join-us", "joinus", "/jobs", "/job-", "/hiring"]
RUMORE_DOMINIO = re.compile(r"(indeed|linkedin|glassdoor|monster|infojobs|stepstone|jobrapido|jooble|adzuna|"
                            r"subito|kijiji|bakeca|trovit|careerjet|jobijoba|hellowork|welcometothejungle|"
                            r"teamtailor|personio|recruitee|greenhouse|lever\.co|workable|smartrecruiters|"
                            r"softgarden|successfactors|myworkdayjobs|icims|bamboohr|breezy|jazzhr|"
                            r"wikipedia|wordpress|blogspot|facebook|google|amazon|apple\.com|microsoft)", re.I)


def _punteggio(path: str) -> int:
    p = path.lower()
    for i, s in enumerate(SPECIFICHE):
        if s in p:
            return i
    return len(SPECIFICHE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--min-url", type=int, default=1, help="pagine carriere minime per dominio")
    a = ap.parse_args()

    per_dominio: dict[str, dict] = {}
    n = 0
    with open(a.csv, newline="") as f:
        for dominio, tld, host, path in csv.reader(f):
            n += 1
            dominio = dominio.lower().strip()
            if not dominio or "." not in dominio or RUMORE_DOMINIO.search(dominio):
                continue
            # il sottodominio carriere e' esso stesso un indizio (jobs.acme.it)
            d = per_dominio.setdefault(dominio, {"tld": tld, "url": None, "punti": 999, "n": 0})
            d["n"] += 1
            punti = _punteggio(path) - (1 if re.match(r"^(jobs?|careers?|karriere|carriere|lavoraconnoi)\.", host) else 0)
            if punti < d["punti"]:
                d["punti"] = punti
                d["url"] = f"https://{host}{path}"
    print(f"righe {n}, domini {len(per_dominio)}")
    per_tld = collections.Counter(d["tld"] for d in per_dominio.values())
    print("per TLD:", dict(per_tld.most_common(12)))

    righe = [(dom, PAESE_TLD.get(d["tld"], d["tld"].upper()), d["url"])
             for dom, d in per_dominio.items() if d["n"] >= a.min_url]
    dsn = os.environ["ATS_DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        stats = {"nuovi": 0, "arricchiti": 0}
        with conn.cursor() as cur:
            for i in range(0, len(righe), 1000):
                parte = righe[i:i + 1000]
                cur.executemany("""
                    INSERT INTO company_domains (domain, country, careers_url, source, status)
                    VALUES (%s, %s, %s, 'cc_carriere', 'pending')
                    ON CONFLICT (domain) DO UPDATE SET
                      country = COALESCE(company_domains.country, EXCLUDED.country),
                      careers_url = COALESCE(company_domains.careers_url, EXCLUDED.careers_url)
                """, parte)
            cur.execute("SELECT count(*) FROM company_domains WHERE source = 'cc_carriere'")
            stats["nuovi"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM company_domains WHERE source <> 'cc_carriere' AND country IS NOT NULL "
                        "AND domain = ANY(%s)", ([r[0] for r in righe],))
            stats["arricchiti"] = cur.fetchone()[0]
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print("Censimento CC carriere:", stats, "(dry-run)" if a.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
