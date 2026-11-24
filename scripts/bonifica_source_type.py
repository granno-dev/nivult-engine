#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psycopg

from nivult.ats.jsonld import _dsn as ats_database_url
from nivult.ats.source_type import classifica_base, classifica_cardinalita, classifica_pubblici


def main() -> int:
    with psycopg.connect(ats_database_url(), autocommit=True, connect_timeout=3) as conn:
        conn.execute("SET idle_in_transaction_session_timeout = '30s'")
        print("base", classifica_base(conn), flush=True)
        print("tenant pubblici inseriti", classifica_pubblici(conn), flush=True)
        print("cardinalita", classifica_cardinalita(conn), flush=True)
        for r in conn.execute("""
            SELECT co.source_type,count(*)::bigint AS tenant,
                   count(j.id) FILTER (WHERE j.expired_at IS NULL)::bigint AS attive
              FROM ats_companies co
              LEFT JOIN ats_jobs j ON j.platform_id=co.platform_id AND j.slug=co.slug
             GROUP BY co.source_type ORDER BY co.source_type
        """):
            print("distribuzione", r, flush=True)
        print("outbox", conn.execute("SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0],
              flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
