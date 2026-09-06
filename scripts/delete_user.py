#!/usr/bin/env python3
"""Cancellazione di un utente su richiesta (GDPR), a lotti.

    python scripts/delete_user.py --user-id <uuid> [--batch-size 5000] [--yes]
    python scripts/delete_user.py --list-pending

Non stampa mai contenuti del CV: solo identificativi e conteggi.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from nivult.config import database_url, safe_dsn  # noqa: E402
from nivult.gdpr import execute_deletion, request_deletion  # noqa: E402


def list_pending(conn) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, user_id, status, requested_at, "
            "jsonb_array_length(pending_storage_keys) "
            "FROM deletion_requests WHERE status <> 'completed' ORDER BY requested_at")
        rows = cur.fetchall()
    if not rows:
        print("nessuna richiesta aperta.")
        return 0
    print(f"{len(rows)} richieste aperte:\n")
    for rid, uid, status, at, keys in rows:
        print(f"  {rid}  utente={uid}  {status:<9} {at:%Y-%m-%d %H:%M}  "
              f"{keys} file da rimuovere")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--user-id")
    ap.add_argument("--batch-size", type=int, default=5000)
    ap.add_argument("--pause", type=float, default=0.0,
                    help="secondi di pausa fra i lotti, per non saturare il database")
    ap.add_argument("--yes", action="store_true", help="non chiedere conferma")
    ap.add_argument("--list-pending", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    dsn = database_url()
    print(f"database: {safe_dsn(dsn)}")

    with psycopg.connect(dsn, autocommit=False) as conn:
        if args.list_pending:
            return list_pending(conn)

        if not args.user_id:
            ap.error("serve --user-id (oppure --list-pending)")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT (SELECT count(*) FROM matches      WHERE user_id=%s), "
                "       (SELECT count(*) FROM digest_items WHERE user_id=%s), "
                "       (SELECT count(*) FROM user_cvs     WHERE user_id=%s)",
                (args.user_id, args.user_id, args.user_id))
            n_matches, n_items, n_cvs = cur.fetchone()

        print(f"\nutente {args.user_id}")
        print(f"  {n_matches} match, {n_items} voci di digest, {n_cvs} CV")
        print("  operazione IRREVERSIBILE.\n")

        if not args.yes:
            if input("scrivi 'cancella' per procedere: ").strip() != "cancella":
                print("annullato.")
                return 1

        request_id = request_deletion(conn, args.user_id)
        print(f"richiesta {request_id} registrata; l'utente non riceverà altri digest.\n")

        totals = execute_deletion(conn, request_id,
                                  batch_size=args.batch_size, pause_seconds=args.pause)
        print("\nriepilogo:")
        for step, count in totals.items():
            print(f"  {step:<24} {count}")

        with conn.cursor() as cur:
            cur.execute("SELECT status, pending_storage_keys FROM deletion_requests "
                        "WHERE id = %s", (request_id,))
            status, keys = cur.fetchone()
        print(f"\nstato richiesta: {status}")
        if status != "completed":
            print(f"  {len(keys)} file su object storage ancora da rimuovere.")
            print("  La richiesta resta aperta finché non sono stati cancellati.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
