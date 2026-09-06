#!/usr/bin/env python3
"""Retention delle offerte morte.

    python scripts/purge_jobs.py --dry-run
    python scripts/purge_jobs.py [--days 60] [--batch-size 1000]
    python scripts/purge_jobs.py --stats

Da mettere in cron una volta al giorno, dopo l'ingestione.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from nivult.config import database_url, safe_dsn  # noqa: E402
from nivult.retention import DEFAULT_RETENTION_DAYS, purge  # noqa: E402


def show_stats(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT family, country, month, jobs_purged, jobs_tombstoned, "
            "       expired_count, removed_count, with_salary_count, avg_lifetime_days "
            "FROM cluster_month_stats_v ORDER BY month DESC, family LIMIT 30")
        rows = cur.fetchall()
    if not rows:
        print("nessun aggregato: la retention non ha ancora eliminato nulla.")
        return 0
    print(f"{'cluster':<28} {'mese':<8} {'canc':>6} {'lapidi':>7} "
          f"{'scad':>6} {'rimo':>6} {'salary':>7} {'vita':>6}")
    for fam, cc, month, purged, tomb, exp, rem, sal, life in rows:
        print(f"{fam[:22]+' '+cc:<28} {month:%Y-%m} {purged:>6} {tomb:>7} "
              f"{exp:>6} {rem:>6} {sal:>7} {life if life is not None else '-':>6}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=DEFAULT_RETENTION_DAYS)
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--pause", type=float, default=0.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true", help="mostra gli aggregati e basta")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    dsn = database_url()
    print(f"database: {safe_dsn(dsn)}")

    with psycopg.connect(dsn, autocommit=False) as conn:
        if args.stats:
            return show_stats(conn)
        totals = purge(conn, older_than_days=args.days, batch_size=args.batch_size,
                       pause_seconds=args.pause, dry_run=args.dry_run)
        print("\nriepilogo:")
        for k, v in totals.items():
            print(f"  {k:<16} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
