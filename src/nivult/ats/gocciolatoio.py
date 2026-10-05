#!/usr/bin/env python3
"""Il gocciolatoio dei tenant: scoperta continua via motori di ricerca
(05/10/2026, idea di Giuseppe — i pattern degli URL degli ATS sono gia'
indicizzati dai motori, noi i pattern li conosciamo perche' li mappiamo).

Perche' esiste: il pilota ha misurato 0,4 tenant NUOVI a query — ma il
SearXNG di casa si strozza dopo ~12 query. La soluzione non e' pagare:
e' girare. La rete pubblica di SearXNG (searx.space) offre decine di
istanze; una query ogni pochi secondi PER ISTANZA, a rotazione, con
backoff su chi si stufa. Gratis, gentile, continuo.

Scrive in ats_companies con discovered_from='searx_pattern', lo stesso
punto della scoperta Common Crawl: ON CONFLICT DO NOTHING, mai doppioni.

    ATS_DATABASE_URL=... python -m nivult.ats.gocciolatoio   (sull'N5)
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from urllib.parse import quote_plus

import httpx
import psycopg

log = logging.getLogger("nivult.ats.gocciolatoio")

NSA = "http://127.0.0.1:8899/search"          # la nostra istanza, prima di tutte
ISTANZE_URL = "https://searx.space/data/instances.json"

PATTERN = {
    "workday": ("wd1.myworkdayjobs.com",
                r"https?://([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com"),
    "workday-wd5": ("wd5.myworkdayjobs.com",
                    r"https?://([a-z0-9-]+)\.wd\d+\.myworkdayjobs\.com"),
    "lever": ("jobs.lever.co", r"https?://jobs\.lever\.co/([a-z0-9-]+)"),
    "greenhouse": ("boards.greenhouse.io",
                   r"https?://boards\.greenhouse\.io/([a-z0-9-]+)"),
    "greenhouse-eu": ("job-boards.eu.greenhouse.io",
                      r"https?://job-boards\.eu\.greenhouse\.io/([a-z0-9-]+)"),
    "smartrecruiters": ("jobs.smartrecruiters.com",
                        r"https?://jobs\.smartrecruiters\.com/([A-Za-z0-9-]+)"),
    "recruitee": ("recruitee.com", r"https?://([a-z0-9-]+)\.recruitee\.com"),
    "ashby": ("jobs.ashbyhq.com", r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9-]+)"),
    "teamtailor": ("teamtailor.com", r"https?://([a-z0-9-]+)\.teamtailor\.com"),
}
# piattaforma reale quando il pattern e' una variante dello stesso ATS
ALIAS = {"workday-wd5": "workday", "greenhouse-eu": "greenhouse"}

KEYWORDS = ["jobs", "careers", "lavoro", "lavora con noi", "stellenangebote",
            "karriere", "emplois", "ofertas de empleo", "vacatures", "vagas",
            "praca", "työpaikat", "jobb", "werken bij", "oferty pracy",
            "italia", "deutschland", "france", "españa", "polska"]


def istanze_sane(cli: httpx.Client) -> list[str]:
    """Le istanze pubbliche vive, con JSON acceso. Si riprova a ogni
    ciclo del pool: chi si stufa esce, chi torna rientra."""
    try:
        d = cli.get(ISTANZE_URL, timeout=20).json()
    except (httpx.HTTPError, ValueError):
        return []
    sane = []
    for url, info in (d.get("instances") or {}).items():
        if info.get("network_type") != "normal":
            continue                       # niente tor/i2p
        if (info.get("http") or {}).get("status_code") != 200:
            continue
        timing = ((info.get("timing") or {}).get("search") or {}
                  ).get("all") or {}
        if (timing.get("mean") or 9) > 4:
            continue                       # lente: non vale la pena
        sane.append(url.rstrip("/"))
    random.shuffle(sane)
    return sane[:30]


class Pool:
    """Rotazione con backoff: successo mantiene il ritmo, zero risultati
    due volte = la istanza si e' stufata e riposa."""

    def __init__(self, nostra: str, pubbliche: list[str]):
        self.stato = {u: {"pronta_il": 0.0, "zitti": 0}
                      for u in [nostra] + pubbliche}

    def prossima(self) -> str | None:
        ora = time.time()
        libere = [u for u, s in self.stato.items() if s["pronta_il"] <= ora]
        if not libere:
            return None
        return min(libere, key=lambda u: self.stato[u]["zitti"])

    def esito(self, url: str, n_risultati: int, errore: bool):
        s = self.stato[url]
        if errore:
            s["pronta_il"] = time.time() + 900        # errore: 15 minuti fuori
        elif n_risultati == 0:
            s["zitti"] += 1
            if s["zitti"] >= 2:
                s["pronta_il"] = time.time() + 600    # muta: 10 minuti fuori
                s["zitti"] = 0
        else:
            s["zitti"] = 0
            s["pronta_il"] = time.time() + 3          # gentile: 3s fra una e l'altra


def gira() -> None:
    dsn = os.environ["ATS_DATABASE_URL"]
    cli = httpx.Client(headers={"User-Agent": "nivult-discovery/1.0"},
                       follow_redirects=True)
    with psycopg.connect(dsn) as c:
        esistenti = {pid: {r[0] for r in c.execute(
            "SELECT slug FROM ats_companies WHERE platform_id = %s", (p,))}
            for pid, p in
            {pid: ALIAS.get(pid, pid) for pid in PATTERN}.items()}

    pool = Pool(NSA, istanze_sane(cli))
    log.info("pool: %d istanze pubbliche + la nostra", len(pool.stato) - 1)
    coda = [(pid, kw) for pid in PATTERN for kw in KEYWORDS]
    giri = 0
    while True:
        if not coda:
            coda = [(pid, kw) for pid in PATTERN for kw in KEYWORDS]
            random.shuffle(coda)
            giri += 1
            if giri % 3 == 0:                # ogni tre giri si ripesca il pool
                nuove = istanze_sane(cli)
                for u in nuove:
                    pool.stato.setdefault(u, {"pronta_il": 0.0, "zitti": 0})
                log.info("pool rinfrescato: %d istanze", len(pool.stato))
        pid, kw = coda.pop(0)
        dominio, rx_str = PATTERN[pid]
        rx = re.compile(rx_str)
        reale = ALIAS.get(pid, pid)
        url = pool.prossima()
        if url is None:
            time.sleep(30)
            continue
        q = f"site:{dominio} {kw}"
        try:
            r = cli.get(f"{url}/search?q={quote_plus(q)}&format=json",
                        timeout=35)
            if r.status_code != 200:
                pool.esito(url, 0, errore=True)
                continue
            risultati = r.json().get("results", [])
        except (httpx.HTTPError, ValueError):
            pool.esito(url, 0, errore=True)
            continue
        pool.esito(url, len(risultati), errore=False)
        nuovi = 0
        da_scrivere = []
        for x in risultati:
            m = rx.search(x.get("url", ""))
            if not m:
                continue
            slug = m.group(1).lower()
            if slug in ("www", "jobs", "boards", "careers", "careersite"):
                continue
            if slug in esistenti[reale]:
                continue
            da_scrivere.append((reale, slug))
        if da_scrivere:
            # UNA connessione per query, non una per tenant: il link
            # Tailscale si paga a round trip
            try:
                with psycopg.connect(dsn) as c:
                    c.executemany(
                        "INSERT INTO ats_companies (platform_id, slug, "
                        "discovered_from) VALUES (%s, %s, 'searx_pattern') "
                        "ON CONFLICT (platform_id, slug) DO NOTHING",
                        da_scrivere)
                for reale, slug in da_scrivere:
                    esistenti[reale].add(slug)
                nuovi = len(da_scrivere)
            except psycopg.Error as e:
                log.warning("insert batch fallita: %s", e)
        if nuovi:
            log.info("%s «%s»: +%d tenant nuovi", reale, kw, nuovi)


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    gira()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
