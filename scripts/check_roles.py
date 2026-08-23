#!/usr/bin/env python3
"""Verifica dei privilegi del ruolo applicativo.

    python scripts/check_roles.py

Prova che nivult_app possa fare il proprio lavoro e nient'altro. Il caso che
conta di più è l'ultimo: che una tabella creata DOPO da una migrazione resti
accessibile all'applicazione. Senza ALTER DEFAULT PRIVILEGES quella verifica
fallisce, e in produzione si manifesterebbe come un errore di permessi subito
dopo un deploy riuscito — un sintomo che non assomiglia alla sua causa.

Solo su database _test/_dev: assegna temporaneamente LOGIN a nivult_app.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

from nivult.config import database_name, database_url, safe_dsn  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
DENIED = "42501"  # insufficient_privilege


def ok(label: str, detail: str = "") -> None:
    PASSED.append(label)
    print(f"  ok    {label}{('  -> ' + detail) if detail else ''}")


def bad(label: str, detail: str) -> None:
    FAILED.append(label)
    print(f"  FAIL  {label} — {detail}")


def allowed(conn, label: str, sql: str) -> None:
    """L'applicazione DEVE poterlo fare."""
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
    except psycopg.Error as exc:
        bad(label, f"negato ({exc.sqlstate}): {str(exc).strip().splitlines()[0]}")
        return
    ok(label)


def denied(conn, label: str, sql: str) -> None:
    """L'applicazione NON deve poterlo fare."""
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
    except psycopg.Error as exc:
        if exc.sqlstate == DENIED:
            ok(label, "permesso negato")
        else:
            bad(label, f"errore sbagliato {exc.sqlstate}: "
                       f"{str(exc).strip().splitlines()[0]}")
        return
    bad(label, "CONSENTITO: il ruolo applicativo può farlo, e non dovrebbe")


def app_dsn() -> str:
    d = conninfo_to_dict(database_url())
    d["user"] = "nivult_app"
    d.pop("password", None)
    return make_conninfo(**d)


def main() -> int:
    db = database_name()
    if not (db.endswith(("_test", "_dev")) or os.environ.get("NIVULT_ALLOW_DESTRUCTIVE") == "1"):
        print(f"rifiuto di girare su '{db}': serve un database _test/_dev.")
        return 2

    print(f"verifica privilegi — {safe_dsn(database_url())}")
    admin = psycopg.connect(database_url(), autocommit=True)
    try:
        with admin.cursor() as cur:
            # Solo in sviluppo: in produzione la password la assegna
            # deploy/setup-roles.sh da variabile d'ambiente.
            cur.execute("ALTER ROLE nivult_app LOGIN")

        app = psycopg.connect(app_dsn())
        try:
            print("\nquello che l'applicazione deve poter fare")
            allowed(app, "SELECT su una tabella", "SELECT count(*) FROM users")
            allowed(app, "INSERT",
                    "INSERT INTO clusters (family,country,daily_credit_cap) "
                    "VALUES ('Priv','IT',10)")
            allowed(app, "UPDATE", "UPDATE clusters SET daily_credit_cap = 11 "
                                   "WHERE family = 'Priv'")
            allowed(app, "DELETE", "DELETE FROM clusters WHERE family = 'Priv'")
            allowed(app, "chiamare una funzione del motore",
                    "SELECT 1 FROM pg_proc WHERE proname = 'cluster_try_consume'")

            print("\nquello che NON deve poter fare")
            denied(app, "CREATE TABLE", "CREATE TABLE intruso (id int)")
            denied(app, "DROP TABLE", "DROP TABLE users")
            denied(app, "ALTER TABLE", "ALTER TABLE users ADD COLUMN password text")
            denied(app, "TRUNCATE", "TRUNCATE matches")
            denied(app, "CREATE INDEX", "CREATE INDEX x ON users (email)")
            denied(app, "creare un ruolo nuovo", "CREATE ROLE scalata NOLOGIN")

            with app.cursor() as cur:
                cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                is_super = cur.fetchone()[0]
            if is_super:
                bad("il ruolo applicativo non è superutente", "è superutente")
            else:
                ok("il ruolo applicativo non è superutente")

            print("\nla trappola: tabelle create dopo, da una migrazione")
            with admin.cursor() as cur:
                cur.execute("CREATE TABLE tabella_futura (id int PRIMARY KEY, v text)")
                cur.execute("INSERT INTO tabella_futura VALUES (1,'x')")
            app.rollback()  # istantanea nuova, deve vedere la tabella appena creata
            allowed(app, "SELECT su una tabella creata DOPO i GRANT",
                    "SELECT count(*) FROM tabella_futura")
            allowed(app, "INSERT su una tabella creata DOPO i GRANT",
                    "INSERT INTO tabella_futura VALUES (2,'y')")
            denied(app, "ma nemmeno lì può fare DDL",
                   "ALTER TABLE tabella_futura ADD COLUMN z int")
        finally:
            app.close()
            with admin.cursor() as cur:
                cur.execute("DROP TABLE IF EXISTS tabella_futura")
                cur.execute("ALTER ROLE nivult_app NOLOGIN")
    finally:
        admin.close()

    print(f"\n{len(PASSED)} superati, {len(FAILED)} falliti")
    if FAILED:
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("OK: il ruolo applicativo può lavorare, non può alterare lo schema")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
