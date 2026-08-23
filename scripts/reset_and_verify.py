#!/usr/bin/env python3
"""Azzera il database, riapplica tutte le migrazioni da zero, verifica.

    python scripts/reset_and_verify.py

Da rilanciare ogni volta che si tocca lo schema. Fa tre cose in sequenza e si
ferma alla prima che fallisce:

  1. DROP SCHEMA public CASCADE  — via anche le estensioni e le impostazioni di
     database, così 0001 viene messa alla prova davvero e non trova il lavoro
     già fatto da un giro precedente;
  2. python -m nivult.migrate up  — tutte le migrazioni dalla tabella vuota;
  3. verify_schema + check_constraints + check_modules — struttura, vincoli, e
     lo strato Python che pilota le funzioni SQL.

DISTRUTTIVO. Gira solo su database che finiscono per _test/_dev, a meno di
NIVULT_ALLOW_DESTRUCTIVE=1.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402

from nivult.config import database_name, database_url, safe_dsn  # noqa: E402

# Impostazioni che 0001 mette a livello di database. Vanno rimosse prima del
# reset, altrimenti il test non prova che la migrazione le imposti davvero:
# le troverebbe già lì dal giro precedente.
DB_SETTINGS = ["timezone", "hnsw.iterative_scan", "hnsw.ef_search"]


def banner(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


def reset(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
        cur.execute("CREATE SCHEMA public")
        cur.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")
        db = psycopg.sql.Identifier(database_name(dsn))
        for setting in DB_SETTINGS:
            cur.execute(
                psycopg.sql.SQL("ALTER DATABASE {} RESET {}").format(
                    db, psycopg.sql.Identifier(*setting.split("."))
                )
            )
    print("  schema public ricreato vuoto, impostazioni di database azzerate")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-behaviour", action="store_true",
                    help="solo reset + migrazioni + verifica strutturale")
    args = ap.parse_args()

    dsn = database_url()
    db = database_name(dsn)
    if not (db.endswith(("_test", "_dev")) or os.environ.get("NIVULT_ALLOW_DESTRUCTIVE") == "1"):
        print(f"rifiuto di azzerare '{db}': non finisce per _test/_dev.\n"
              f"Se è davvero quello che vuoi: NIVULT_ALLOW_DESTRUCTIVE=1")
        return 2

    started = time.monotonic()
    print(f"reset e verifica — {safe_dsn(dsn)}")

    banner("1/3  azzeramento")
    reset(dsn)

    banner("2/3  migrazioni da zero")
    from nivult.migrate import main as migrate_main

    if migrate_main(["up"]) != 0:
        print("\nFALLITO: le migrazioni non si applicano su database vuoto")
        return 1

    banner("3/3  verifica")
    import verify_schema

    if verify_schema.main() != 0:
        print("\nFALLITO: schema non conforme")
        return 1

    if not args.skip_behaviour:
        print()
        import check_constraints

        if check_constraints.main() != 0:
            print("\nFALLITO: un vincolo non rifiuta ciò che dovrebbe")
            return 1

        print()
        import check_modules

        if check_modules.main() != 0:
            print("\nFALLITO: lo strato Python non si comporta come deve")
            return 1

    elapsed = time.monotonic() - started
    banner(f"OK — schema ricostruito e verificato in {elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
