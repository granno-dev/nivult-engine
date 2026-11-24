#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nivult.ats.backfill import attendi
from nivult.ats.company_names import board_url, nome_da_html, nomi_da_raw, pulisci
from nivult.ats.jsonld import _dsn

PIATTAFORME = ("icims", "successfactors", "lever", "ashby", "workday",
               "workable", "bamboohr", "teamtailor", "rippling", "personio")


def _tenant_mancanti(conn, platform_id: str) -> int:
    return conn.execute("""
        SELECT count(DISTINCT j.slug) FROM ats_jobs j
        LEFT JOIN ats_companies co ON co.platform_id=j.platform_id AND co.slug=j.slug
        WHERE j.platform_id=%s AND co.company_name IS NULL
    """, (platform_id,)).fetchone()[0]


def _processa(conn, scrittura, platform_id: str, client: httpx.Client) -> dict:
    mancanti = _tenant_mancanti(scrittura, platform_id)
    stats = {"mancanti": mancanti, "raw": 0, "domain": 0, "board": 0,
             "residui": 0, "http_disabilitato": mancanti > 2000}
    if stats["http_disabilitato"]:
        print("HTTP oltre 2000 tenant, disabilitato", platform_id, mancanti, flush=True)
    query = """
        SELECT j.slug,j.raw,(co.slug IS NULL) AS manca_riga,co.site_domain,
               co.company_name_source,co.wd_server,co.wd_instance
          FROM ats_jobs j
          LEFT JOIN ats_companies co ON co.platform_id=j.platform_id AND co.slug=j.slug
         WHERE j.platform_id=%s AND co.company_name IS NULL
         ORDER BY j.slug,j.id
    """
    batch = []
    corrente = None
    nomi = []
    meta = None

    def flush():
        nonlocal batch
        if not batch:
            return
        attendi(scrittura)
        with scrittura.transaction(), scrittura.cursor() as cur:
            cur.executemany("""
                INSERT INTO ats_companies(platform_id,slug,company_name,
                    company_name_source,source_type,discovered_from)
                VALUES (%s,%s,%s,%s,'direct','backfill_nomi')
                ON CONFLICT (platform_id,slug) DO UPDATE SET
                  company_name=EXCLUDED.company_name,
                  company_name_source=EXCLUDED.company_name_source
            """, batch)
        batch = []

    def risolvi(slug, raws, info):
        nonlocal batch
        _manca_riga, site_domain, name_source, wd_server, wd_instance = info
        candidati = {n.casefold(): n for raw in raws for n in nomi_da_raw(raw)}
        nome = next(iter(candidati.values())) if len(candidati) == 1 else None
        fonte = "raw" if nome else None
        if not nome:
            dominio = site_domain if site_domain and "." in site_domain else slug
            if "." in dominio:
                riga = scrittura.execute(
                    "SELECT company_name FROM company_domains WHERE domain=%s",
                    (dominio,)).fetchone()
                nome = pulisci(riga[0] if riga else None)
                fonte = "domain" if nome else None
        if not nome and not name_source and not stats["http_disabilitato"]:
            url = board_url(platform_id, slug)
            if platform_id == "workday" and wd_server:
                url = (f"https://{slug}.{wd_server}.myworkdayjobs.com/"
                       f"{wd_instance or ''}")
            if url:
                try:
                    r = client.get(url)
                    if r.status_code == 200:
                        nome = nome_da_html(r.text)
                        fonte = "board" if nome else None
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
        if nome:
            stats[fonte] += 1
            batch.append((platform_id, slug, nome, fonte))
            if len(batch) >= 100:
                flush()
        else:
            stats["residui"] += 1

    with conn.cursor(name=f"nomi_{platform_id}") as cur:
        cur.itersize = 1000
        cur.execute(query, (platform_id,))
        for slug, raw, manca, site, source, server, instance in cur:
            if corrente is None:
                corrente = slug
            if slug != corrente:
                risolvi(corrente, nomi, meta)
                nomi = []
                corrente = slug
            if raw:
                nomi.append(raw)
            meta = (manca, site, source, server, instance)
        if corrente is not None:
            risolvi(corrente, nomi, meta)
    flush()
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--piattaforma", choices=PIATTAFORME)
    args = ap.parse_args()
    with psycopg.connect(_dsn(), autocommit=True, connect_timeout=3) as scrittura, \
            psycopg.connect(_dsn(), connect_timeout=3) as lettura, \
            httpx.Client(timeout=20, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"}) as client:
        scrittura.execute("SET statement_timeout = '30min'")
        lettura.execute("SET statement_timeout = '30min'")
        for platform_id in ((args.piattaforma,) if args.piattaforma else PIATTAFORME):
            print("inizio", platform_id, flush=True)
            stats = _processa(lettura, scrittura, platform_id, client)
            print("fine", platform_id, stats, flush=True)
            lettura.rollback()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
