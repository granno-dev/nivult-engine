"""Raccolta domini e tenant dai crediti Fantastic Jobs (26/09/2026).

PERCHE' ESISTE. 22.870 tenant attivi non hanno il dominio aziendale, e
migliaia di aziende che assumono non sono ancora nel motore. Fantastic
Jobs regala 5.000 crediti: ogni record di /active-ats porta
`organization_url` (il sito dell'azienda) e, per alcune piattaforme, il
tenant (`source_slug` o il sottodominio di `source_domain`).

LA REGOLA DEL PERIMETRO (inviolabile): Fantastic e' un AGGREGATORE.
Di ogni record prendiamo SOLO nome azienda, URL del sito e aggancio al
tenant ATS. Il contenuto dell'annuncio non si legge e non si salva —
la stessa regola del radar Indeed: il link diretto si', il contenuto mai.

Costi: 1 credito per record + 1 per richiesta. Le pagine sono da 1000.
Il budget si dichiara nel codice e lo script si ferma da solo.

Uso (sul server, la chiave sta in /opt/nivult/.env):
    python scripts/fantastic_domini.py            # esegue e scrive
    python scripts/fantastic_domini.py --dry      # stima senza scrivere
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from urllib.parse import urlparse

import httpx
import psycopg

log = logging.getLogger("nivult.fantastic_domini")

BASE = "https://data.fantastic.jobs/v1"

# Il budget del 26/09/2026: 5.000 crediti regalati. Una pagina da 1000
# costa 1 richiesta + fino a 1000 record. La ripartizione punta dove il
# bacino e' grande E la nostra copertura di domini e' piu' magra.
PIANO = [("United States", 2000), ("United Kingdom", 800),
         ("Canada", 500), ("Germany", 500), ("France", 400),
         ("Australia", 300), ("Sweden", 200), ("Switzerland", 150),
         ("Italy", 100)]

# source di Fantastic -> (nostra piattaforma, come ricavare lo slug).
# join.com non porta il tenant (id numerico interno): il dominio resta
# utile ma l'aggancio al tenant no.
_RX_PERSONIO = re.compile(r"^([a-z0-9-]+)\.jobs\.personio\.com$", re.I)
_RX_ORACLE = re.compile(r"^([a-z0-9-]+)\.fa\.[a-z0-9]+\.oraclecloud\.com$", re.I)
_RX_WORKDAY = re.compile(r"^([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com$", re.I)
_RX_ICIMS = re.compile(r"^([a-z0-9-]+)\.icims\.com$", re.I)


def _tenant(source: str, slug: str | None, dominio: str) -> tuple[str, str] | None:
    """(platform_id, slug) dal record, o None se la fonte non lo dice."""
    s = (source or "").lower().replace(".com", "")
    if slug:
        if s in ("greenhouse", "lever", "ashby", "smartrecruiters",
                 "workable", "breezy", "teamtailor", "recruitee"):
            return s, slug
    d = (dominio or "").lower()
    for rx, pid in ((_RX_PERSONIO, "personio"), (_RX_ORACLE, "oracle"),
                    (_RX_ICIMS, "icims")):
        m = rx.match(d)
        if m:
            return pid, m.group(1)
    m = _RX_WORKDAY.match(d)
    if m:
        return "workday", m.group(1)
    return None


def _dominio_azienda(url: str | None) -> str | None:
    """www.gfn.de -> gfn.de. Niente da organization_url assente o rotta."""
    if not url or "://" not in url:
        return None
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except ValueError:
        return None   # URL deforme («Invalid IPv6 URL» misurato il 26/09)
    if host.startswith("www."):
        host = host[4:]
    # i domini delle piattaforme non sono domini d'azienda
    if not host or host.endswith(("join.com", "jobs.personio.com",
                                  "myworkdayjobs.com", "oraclecloud.com",
                                  "icims.com", "greenhouse.io",
                                  "lever.co", "ashbyhq.com")):
        return None
    return host


def raccolta(key: str) -> list[dict]:
    """Scarica le pagine del piano e restituisce i record distillati."""
    visti: set[tuple] = set()
    out: list[dict] = []
    spesi = 0
    with httpx.Client(timeout=30, headers={
            "Authorization": f"Bearer {key}",
            "accept": "application/json"}) as cli:
        for paese, budget in PIANO:
            presi = 0
            offset = 0
            while presi < budget:
                limite = min(1000, budget - presi)
                r = cli.get(BASE + "/active-ats", params={
                    "location": paese, "time_frame": "7d",
                    "limit": limite, "offset": offset,
                    "include_basic_organization_details": "true"})
                if r.status_code != 200:
                    # quota finita o guasto: ci si ferma, non si insiste
                    log.warning("%s: HTTP %s — stop", paese, r.status_code)
                    break
                recs = r.json()
                if isinstance(recs, dict):
                    recs = recs.get("results") or recs.get("data") or []
                if not recs:
                    break
                spesi += 1 + len(recs)   # 1 richiesta + 1 per record
                presi += len(recs)
                offset += len(recs)
                for j in recs:
                    t = _tenant(j.get("source"), j.get("source_slug"),
                                j.get("source_domain") or "")
                    dom = _dominio_azienda(j.get("organization_url"))
                    chiave = (t, dom, j.get("organization"))
                    if chiave in visti:
                        continue
                    visti.add(chiave)
                    if t or dom:
                        out.append({"tenant": t, "dominio": dom,
                                    "nome": j.get("organization")})
                time.sleep(1.1)          # il cliente storico fa 1 req/s
            log.info("%s: %d record letti", paese, presi)
    log.info("crediti spesi (stima): %d", spesi)
    return out


def applica(dsn: str, righe: list[dict], dry: bool) -> dict:
    """Scrive: domini sui tenant che li aspettano, tenant nuovi in coda."""
    st = {"domini_riempiti": 0, "tenant_nuovi": 0, "tenant_presenti": 0,
          "domini_orfani": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        for r in righe:
            t, dom = r["tenant"], r["dominio"]
            if t:
                pid, slug = t
                trovato = c.execute(
                    "SELECT id, site_domain FROM ats_companies "
                    "WHERE platform_id = %s AND slug = %s",
                    (pid, slug)).fetchone()
                if trovato:
                    st["tenant_presenti"] += 1
                    if dom and not trovato[1] and not dry:
                        c.execute(
                            "UPDATE ats_companies SET site_domain = %s, "
                            "site_domain_source = 'fantastic.jobs' "
                            "WHERE id = %s", (dom, trovato[0]))
                        st["domini_riempiti"] += 1
                elif not dry:
                    c.execute(
                        "INSERT INTO ats_companies (platform_id, slug, "
                        "company_name, site_domain, site_domain_source, "
                        "discovered_from) VALUES (%s, %s, %s, %s, %s, %s) "
                        "ON CONFLICT (platform_id, slug) DO NOTHING",
                        (pid, slug, r["nome"], dom,
                         "fantastic.jobs" if dom else None,
                         "fantastic.jobs"))
                    st["tenant_nuovi"] += 1
            elif dom:
                st["domini_orfani"] += 1   # utili al cacciatore di domini
    return st


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)-8s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    key = os.environ.get("FANTASTIC_API_KEY", "")
    if not key:
        for f in ("/opt/nivult/.env",):
            m = re.search(r"^FANTASTIC_API_KEY=(.*)$",
                          open(f).read(), re.M) if os.path.exists(f) else None
            if m:
                key = m.group(1).strip().strip("'\"")
                break
    if not key:
        sys.exit("FANTASTIC_API_KEY mancante")
    dsn = os.environ.get("ATS_DATABASE_URL")
    if not dsn:
        sys.exit("ATS_DATABASE_URL mancante")

    righe = raccolta(key)
    with open("/tmp/fantastic_domini.jsonl", "w", encoding="utf-8") as f:
        for r in righe:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    st = applica(dsn, righe, args.dry)
    print(f"fantastic_domini: {st} — dettaglio in /tmp/fantastic_domini.jsonl")
    return 0


if __name__ == "__main__":
    sys.exit(main())
