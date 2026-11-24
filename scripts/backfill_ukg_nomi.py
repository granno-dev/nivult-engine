#!/usr/bin/env python3
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import psycopg

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nivult.ats.backfill import attendi
from nivult.ats.jsonld import _dsn as ats_database_url
from nivult.ats.ukg_nomi import leggi_board, pulisci_nome


def main() -> int:
    stats = {"tenant": 0, "risolti": 0, "null": 0, "errori": 0, "batch": 0}
    with psycopg.connect(ats_database_url(), autocommit=True, connect_timeout=3) as conn:
        conn.execute("SET statement_timeout = '60s'")
        righe = conn.execute("""
            SELECT co.slug,count(j.id) FILTER (WHERE j.expired_at IS NULL) AS attive
              FROM ats_companies co
              LEFT JOIN ats_jobs j ON j.platform_id=co.platform_id AND j.slug=co.slug
             WHERE co.platform_id='ukg'
               AND (co.company_name ~* '(jobboard|https?://|/)'
                    OR co.company_name ~ '^[A-Z]{2,4}[0-9]{3,4}'
                    OR co.company_name ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
             GROUP BY co.slug ORDER BY co.slug
        """).fetchall()
        batch = []
        attive_batch = 0

        def flush():
            nonlocal batch, attive_batch
            if not batch:
                return
            attendi(conn)
            with conn.transaction(), conn.cursor() as cur:
                cur.executemany(
                    "UPDATE ats_companies SET company_name=%s, "
                    "site_domain=COALESCE(site_domain,%s) "
                    "WHERE platform_id='ukg' AND slug=%s", batch)
            stats["batch"] += 1
            batch = []
            attive_batch = 0

        def fetch(client, riga):
            slug, attive = riga
            try:
                return slug, attive, leggi_board(client, slug), None
            except Exception as exc:  # noqa: BLE001
                return slug, attive, None, type(exc).__name__

        with httpx.Client(timeout=10, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as client, \
                ThreadPoolExecutor(max_workers=2) as pool:
            for inizio in range(0, len(righe), 20):
                for slug, attive, meta, errore in pool.map(
                        lambda riga: fetch(client, riga), righe[inizio:inizio + 20]):
                    stats["tenant"] += 1
                    if errore:
                        stats["errori"] += 1
                        print("errore", slug, errore, flush=True)
                        continue
                    nome = meta["company_name"]
                    if not nome and meta["site_domain"]:
                        db_nome = conn.execute(
                            "SELECT company_name FROM company_domains WHERE domain=%s",
                            (meta["site_domain"],)).fetchone()
                        nome = pulisci_nome(db_nome[0] if db_nome else None)
                    if nome:
                        stats["risolti"] += 1
                    else:
                        stats["null"] += 1
                    batch.append((nome, meta["site_domain"], slug))
                    attive_batch += attive
                    if len(batch) >= 10 or attive_batch >= 1000:
                        flush()
                    if stats["tenant"] % 50 == 0:
                        print("progresso", stats, flush=True)
        flush()
        print("finale", stats, flush=True)
        print("outbox", conn.execute("SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0],
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
