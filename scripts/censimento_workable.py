#!/usr/bin/env python3
"""I datori Workable di un paese, dalla bacheca pubblica di Workable stessa.

jobs.workable.com e' la vetrina che Workable tiene per i propri clienti:
non un aggregatore terzo, ma il fornitore dell'ATS che elenca le
offerte dei suoi tenant. La sua API pubblica (`/api/v1/jobs?location=
Italy`, paginata con `pageToken`) da' per ogni offerta l'azienda con il
suo SITO WEB. Lo slug di `apply.workable.com/{slug}` non c'e': lo trova
il detector visitando il sito (il link alla board sta in homepage o
nella pagina carriere, e il registro lo riconosce). Misurato il
07/09/2026: Italia 1.811 offerte sulla bacheca contro 369 nostre attive
Workable in Italia; Germania 3.811, Spagna 1.879, Portogallo 1.757.

    ATS_DATABASE_URL=... python scripts/censimento_workable.py --paesi Italy,Germany --dry-run

I domini entrano in company_domains con source='workable_board', il
paese, e status 'pending' (o restano com'erano se gia' censiti, ma con
il paese riempito). Da li' il giro e' quello di sempre: detector →
tenant → runner.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from urllib.parse import urlparse

import httpx
import psycopg

log = logging.getLogger("censimento_workable")
API = "https://jobs.workable.com/api/v1/jobs"
PAESI = {"Italy": "IT", "Germany": "DE", "France": "FR", "Spain": "ES", "Netherlands": "NL",
         "Portugal": "PT", "Belgium": "BE", "Austria": "AT", "Switzerland": "CH", "Poland": "PL",
         "Sweden": "SE", "Denmark": "DK", "Norway": "NO", "Finland": "FI", "Ireland": "IE",
         "United Kingdom": "GB", "Greece": "GR", "Czechia": "CZ", "Romania": "RO", "Hungary": "HU"}


def _dominio(sito: str | None) -> str | None:
    if not sito:
        return None
    if not sito.startswith("http"):
        sito = "https://" + sito
    host = (urlparse(sito).netloc or "").lower().split(":")[0]
    host = host.removeprefix("www.")
    if not host or "." not in host or "workable.com" in host:
        return None
    return host


def censisci(paese: str, cli: httpx.Client, max_pagine: int = 500) -> dict[str, str]:
    """{dominio: nome} dei datori con offerte in `paese`."""
    aziende: dict[str, str] = {}
    token = None
    for _ in range(max_pagine):
        params = {"query": "", "location": paese}
        if token:
            params["pageToken"] = token
        try:
            r = cli.get(API, params=params)
        except httpx.HTTPError as exc:
            log.warning("%s: %s", paese, exc)
            break
        if r.status_code == 429:
            time.sleep(10)
            continue
        if r.status_code != 200:
            break
        d = r.json()
        for j in d.get("jobs", []):
            az = j.get("company") or {}
            dom = _dominio(az.get("website"))
            if dom:
                aziende.setdefault(dom, az.get("title") or "")
        token = d.get("nextPageToken")
        if not token or not d.get("jobs"):
            break
        time.sleep(0.5)
    return aziende


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--paesi", default=",".join(PAESI))
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    dsn = os.environ["ATS_DATABASE_URL"]
    tot = {"aziende": 0, "nuove": 0}
    with httpx.Client(timeout=30, headers={"User-Agent": "Mozilla/5.0 (nivult-ats)"}) as cli, \
            psycopg.connect(dsn) as conn:
        for paese in a.paesi.split(","):
            paese = paese.strip()
            iso = PAESI.get(paese)
            if not iso:
                log.warning("paese sconosciuto: %s", paese)
                continue
            aziende = censisci(paese, cli)
            nuove = 0
            with conn.cursor() as cur:
                for dom, nome in aziende.items():
                    cur.execute("""
                        INSERT INTO company_domains (domain, company_name, country, source, status)
                        VALUES (%s, %s, %s, 'workable_board', 'pending')
                        ON CONFLICT (domain) DO UPDATE SET
                          country = COALESCE(company_domains.country, EXCLUDED.country),
                          company_name = COALESCE(company_domains.company_name, EXCLUDED.company_name)
                        RETURNING (xmax = 0)""", (dom, nome or None, iso))
                    r = cur.fetchone()
                    nuove += 1 if r and r[0] else 0
            log.info("%s: %d datori, %d domini nuovi", paese, len(aziende), nuove)
            tot["aziende"] += len(aziende)
            tot["nuove"] += nuove
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print("Censimento Workable:", tot, "(dry-run)" if a.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
