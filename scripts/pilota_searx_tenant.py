#!/usr/bin/env python3
"""Pilota: scoperta tenant ATS via ricerca a pattern (05/10/2026).

L'idea di Giuseppe: i motori hanno gia' indicizzato le career page;
noi conosciamo i pattern URL degli ATS — site:/inurl: li pesca. Si
misura il RENDIMENTO: tenant nuovi per query, contro i 69k gia' mappati.
Se rende, diventa un passo del ciclo di scoperta; se no, muore qui.

Gira sull'N5 contro il SearXNG locale (metasearch: niente dipendenza da
un singolo motore, e Google CSE muore il 01/01/2027 comunque).

    ATS_DSN=... python scripts/pilota_searx_tenant.py
"""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import quote_plus, urlparse

import httpx
import psycopg

SEARX = os.environ.get("SEARX_URL", "http://127.0.0.1:8899/search")
PAUSA = float(os.environ.get("PAUSA_SEARX", "2.0"))

# i pattern: piattaforma -> (dominio query, regex del tenant nell'URL)
PATTERN = {
    "workday": ("wd1.myworkdayjobs.com",
                r"https?://([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com"),
    "lever": ("jobs.lever.co", r"https?://jobs\.lever\.co/([a-z0-9-]+)"),
    "greenhouse": ("boards.greenhouse.io",
                   r"https?://boards\.greenhouse\.io/([a-z0-9-]+)"),
    "smartrecruiters": ("jobs.smartrecruiters.com",
                        r"https?://jobs\.smartrecruiters\.com/([A-Za-z0-9-]+)"),
    "recruitee": ("recruitee.com", r"https?://([a-z0-9-]+)\.recruitee\.com"),
    "ashby": ("jobs.ashbyhq.com", r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9-]+)"),
}

# la stessa query in piu' lingue pesca tenant in piu' paesi: il motore
# classifica per rilevanza locale
KEYWORDS = ["jobs", "careers", "lavoro", "stellenangebote", "karriere",
            "emplois", "ofertas de empleo", "vacatures", "vagas",
            "praca", "työpaikat", "jobb"]


def cerca(cli: httpx.Client, q: str) -> list[str]:
    try:
        r = cli.get(f"{SEARX}?q={quote_plus(q)}&format=json", timeout=30)
        if r.status_code != 200:
            return []
        return [x.get("url", "") for x in r.json().get("results", [])]
    except (httpx.HTTPError, ValueError):
        return []


def main() -> int:
    dsn = os.environ.get("ATS_DATABASE_URL") or os.environ["ATS_DSN"]
    esistenti: dict[str, set[str]] = {}
    with psycopg.connect(dsn) as c:
        for pid in PATTERN:
            esistenti[pid] = {r[0] for r in c.execute(
                "SELECT slug FROM ats_companies WHERE platform_id = %s", (pid,))}

    resoconto = {}
    trovati: dict[str, set[str]] = {p: set() for p in PATTERN}
    nuovi: dict[str, set[str]] = {p: set() for p in PATTERN}
    with httpx.Client(headers={"User-Agent": "nivult-pilota/0.1"}) as cli:
        for pid, (dominio, rx_str) in PATTERN.items():
            rx = re.compile(rx_str)
            n_query = 0
            for kw in KEYWORDS:
                q = f"site:{dominio} {kw}"
                n_query += 1
                for url in cerca(cli, q):
                    m = rx.search(url)
                    if not m:
                        continue
                    slug = m.group(1).lower()
                    if slug in ("www", "jobs", "boards", "careers"):
                        continue
                    trovati[pid].add(slug)
                    if slug not in esistenti[pid]:
                        nuovi[pid].add(slug)
                time.sleep(PAUSA)
            resoconto[pid] = {"query": n_query, "trovati": len(trovati[pid]),
                              "nuovi": len(nuovi[pid])}
            print(f"{pid}: {n_query} query, {len(trovati[pid])} tenant, "
                  f"{len(nuovi[pid])} NUOVI", flush=True)

    print("\n=== RENDIMENTO ===")
    tq = sum(r["query"] for r in resoconto.values())
    tn = sum(r["nuovi"] for r in resoconto.values())
    print(json.dumps(resoconto, indent=1))
    print(f"totale: {tq} query -> {tn} tenant NUOVI "
          f"({tn / max(1, tq):.2f} nuovi per query)")
    print("\ntenant nuovi (campione):")
    for pid in PATTERN:
        esempi = sorted(nuovi[pid])[:10]
        if esempi:
            print(f"  {pid}: {', '.join(esempi)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
