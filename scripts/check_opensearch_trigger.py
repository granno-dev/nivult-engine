#!/usr/bin/env python3
"""Test dei trigger outbox aziende su database locale isolato."""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
from psycopg import sql

ROOT = Path(__file__).resolve().parents[1]
ADMIN = os.environ.get("TRIGGER_TEST_ADMIN_URL", "postgresql://127.0.0.1:5432/postgres")
DB = "nivult_trigger_test"
DSN = f"postgresql://127.0.0.1:5432/{DB}"
PASSED = []
FAILED = []


def check(label, got, expected):
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}: {got!r} != {expected!r}")


def main() -> int:
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(DB)))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(DB)))
    try:
        with psycopg.connect(DSN) as conn, conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE ats_companies (
                  platform_id text NOT NULL,
                  slug text NOT NULL,
                  company_name text,
                  PRIMARY KEY (platform_id, slug));
                CREATE TABLE ats_jobs (
                  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                  platform_id text NOT NULL,
                  slug text NOT NULL,
                  expired_at timestamptz);
                CREATE TABLE ats_jobs_search_outbox (
                  job_id uuid PRIMARY KEY,
                  op text NOT NULL,
                  queued_at timestamptz NOT NULL DEFAULT now());
                CREATE FUNCTION ats_jobs_search_azienda()
                RETURNS trigger LANGUAGE plpgsql AS $$
                DECLARE
                  pid text := coalesce(NEW.platform_id, OLD.platform_id);
                  s text := coalesce(NEW.slug, OLD.slug);
                BEGIN
                  INSERT INTO ats_jobs_search_outbox(job_id, op)
                  SELECT id, 'upsert' FROM ats_jobs WHERE platform_id=pid AND slug=s
                  ON CONFLICT(job_id) DO UPDATE SET op='upsert', queued_at=now();
                  RETURN coalesce(NEW, OLD);
                END;
                $$;
                CREATE FUNCTION ats_tocca_azienda_collegata()
                RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RETURN coalesce(NEW, OLD); END;
                $$;
            """)
            cur.execute((ROOT / "migrations/0093_opensearch_azienda_noop.sql").read_text())
            cur.execute((ROOT / "migrations/0094_opensearch_aziende_attive_source_type.sql").read_text())
            cur.execute((ROOT / "migrations/0095_opensearch_azienda_insert_null.sql").read_text())
            cur.execute((ROOT / "migrations/0097_delta_azienda_no_insert.sql").read_text())
            cur.execute("INSERT INTO ats_jobs(platform_id, slug, expired_at) "
                        "VALUES ('p', 's', NULL), ('p', 's', NULL), ('p', 's', now())")
            cur.execute("INSERT INTO ats_companies(platform_id, slug, company_name) "
                        "VALUES ('p', 's', 'Nome')")
            check("INSERT azienda accoda solo le attive", cur.execute(
                "SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0], 2)
            cur.execute("INSERT INTO ats_companies(platform_id, slug, company_name) "
                        "VALUES ('p', 'null', NULL)")
            check("INSERT azienda senza nome non accoda", cur.execute(
                "SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0], 2)
            cur.execute("DELETE FROM ats_jobs_search_outbox")
            cur.execute("UPDATE ats_companies SET company_name='Nome' "
                        "WHERE platform_id='p' AND slug='s'")
            check("UPDATE a valore identico non accoda", cur.execute(
                "SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0], 0)
            cur.execute("UPDATE ats_companies SET source_type='aggregator' "
                        "WHERE platform_id='p' AND slug='s'")
            check("source_type non accoda", cur.execute(
                "SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0], 0)
            check("source_type ammette aggregator", cur.execute(
                "SELECT source_type FROM ats_companies WHERE platform_id='p' AND slug='s'"
                ).fetchone()[0], "aggregator")
            cur.execute("UPDATE ats_companies SET company_name='Nuovo nome' "
                        "WHERE platform_id='p' AND slug='s'")
            check("UPDATE company_name accoda solo le attive", cur.execute(
                "SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0], 2)
            cur.execute("DELETE FROM ats_jobs_search_outbox")
            cur.execute("DELETE FROM ats_companies WHERE platform_id='p' AND slug='s'")
            check("DELETE azienda accoda solo le attive", cur.execute(
                "SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0], 2)
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(DB)))
    print(f"{len(PASSED)} controlli passati, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
