#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Json

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nivult.ats.source_type import (
    COMPANY_SQL,
    classifica,
    classifica_base,
    classifica_cardinalita,
    classifica_pubblici,
    da_blocklist,
)

ADMIN = os.environ.get("SOURCE_TYPE_TEST_ADMIN_URL", "postgresql://127.0.0.1:5432/postgres")
DB = "nivult_source_type_test"
DSN = f"postgresql://127.0.0.1:5432/{DB}"
PASSED = []
FAILED = []


def check(label, got, expected=True):
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
        check("blocklist normalizza www", da_blocklist("www.indeed.com"))
        check("cardinalita cinque nomi", classifica("jsonld", "x.test", nomi=5), "aggregator")
        check("500 offerte senza nomi", classifica("jsonld", "x.test", offerte=500), "aggregator")
        check("sameAs multi dominio", classifica(
            "jsonld", "x.test", sameas_esterni=2, offerte_sameas_esterno=5), "aggregator")
        check("direct sotto soglia", classifica("jsonld", "x.test", offerte=4, nomi=1), "direct")
        check("agenzie staffing", classifica("agenzie", "randstad"), "staffing")
        check("ente pubblico", classifica("nav", "nav"), "public_board")

        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("""
                CREATE TABLE ats_companies (
                  platform_id text NOT NULL, slug text NOT NULL, company_name text,
                  site_domain text, last_fetch_at timestamptz, discovered_from text,
                  PRIMARY KEY (platform_id, slug));
                CREATE TABLE ats_jobs (
                  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                  platform_id text NOT NULL, slug text NOT NULL, raw jsonb,
                  expired_at timestamptz);
                CREATE TABLE ats_jobs_search_outbox (
                  job_id uuid PRIMARY KEY, op text NOT NULL,
                  queued_at timestamptz NOT NULL DEFAULT now());
                CREATE FUNCTION ats_jobs_search_azienda() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RETURN coalesce(NEW, OLD); END;
                $$;
            """)
            conn.execute((ROOT / "migrations/0093_opensearch_azienda_noop.sql").read_text())
            conn.execute((ROOT / "migrations/0094_opensearch_aziende_attive_source_type.sql").read_text())
            conn.execute((ROOT / "migrations/0095_opensearch_azienda_insert_null.sql").read_text())
            tenant = [("jsonld", "indeed.com", "Indeed"), ("jsonld", "many.test", "Portal"),
                      ("jsonld", "direct.test", "Direct"), ("jsonld", "sameas.test", None),
                      ("jsonld", "zero.test", None), ("jsonld", "randstad.de", "Randstad"),
                      ("agenzie", "randstad", "Randstad"), ("nav", "nav", None)]
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO ats_companies(platform_id,slug,company_name) VALUES (%s,%s,%s)",
                    tenant)
                cur.executemany(
                    "INSERT INTO ats_jobs(platform_id,slug,raw) VALUES ('jsonld','many.test',%s)",
                    [(Json({"company": {"name": f"N{i}"}}),) for i in range(5)])
            conn.execute("INSERT INTO ats_jobs(platform_id,slug,raw) "
                         "SELECT 'jsonld','direct.test',%s FROM generate_series(1,4)",
                         (Json({"company": {"name": "Direct Raw"}}),))
            conn.execute("INSERT INTO ats_jobs(platform_id,slug,raw) "
                         "SELECT 'jsonld','sameas.test',jsonb_build_object('hiringOrganization',"
                         "jsonb_build_object('sameAs', 'https://' || (1 + i % 2) || '.example/job/' || i)) "
                         "FROM generate_series(1,5) i")
            conn.execute("INSERT INTO ats_jobs(platform_id,slug,raw) "
                         "SELECT 'jsonld','zero.test','{}' FROM generate_series(1,500)")
            conn.execute("INSERT INTO ats_jobs(platform_id,slug) VALUES ('francetravail','ft')")
            base = classifica_base(conn)
            check("classificazione base aggregator", base["aggregator"], 1)
            check("classificazione base staffing", base["staffing"], 2)
            check("classificazione base public", base["public_board"], 1)
            check("tenant pubblico mancante inserito", classifica_pubblici(conn), 1)
            check("tenant pubblico classificato", conn.execute(
                "SELECT source_type FROM ats_companies WHERE platform_id='francetravail'"
                ).fetchone()[0], "public_board")
            card = classifica_cardinalita(conn)
            check("cardinalita promuove tre tenant", card["aggregator"], 3)
            check("tenant sotto soglia resta direct", card["direct"], 1)
            finali = dict(conn.execute("SELECT slug,source_type FROM ats_companies "
                                       "WHERE platform_id='jsonld'").fetchall())
            check("many aggregator", finali["many.test"], "aggregator")
            check("zero aggregator", finali["zero.test"], "aggregator")
            check("sameas aggregator", finali["sameas.test"], "aggregator")
            check("direct resta direct", finali["direct.test"], "direct")
            check("randstad staffing", finali["randstad.de"], "staffing")
            check("source_type non riempie outbox", conn.execute(
                "SELECT count(*) FROM ats_jobs_search_outbox").fetchone()[0], 0)

            conn.execute("INSERT INTO ats_companies(platform_id,slug,company_name,source_type) VALUES ('jsonld','agg.test','Jobtarget','aggregator')")
            conn.execute("INSERT INTO ats_jobs(platform_id,slug,raw) VALUES "
                         "('jsonld','agg.test',%s)", (Json({"company": {"name": " Real Corp "}}),))
            check("aggregator privilegia datore raw", conn.execute(
                f"SELECT {COMPANY_SQL} FROM ats_jobs j JOIN ats_companies co "
                "ON co.platform_id=j.platform_id AND co.slug=j.slug WHERE j.slug='agg.test'").fetchone()[0],
                "Real Corp")
            conn.execute("INSERT INTO ats_companies(platform_id,slug,company_name,source_type) VALUES ('jsonld','direct2.test',' Direct ','direct')")
            conn.execute("INSERT INTO ats_jobs(platform_id,slug,raw) VALUES "
                         "('jsonld','direct2.test',%s)", (Json({"company": {"name": "Other"}}),))
            check("direct privilegia nome tenant ripulito", conn.execute(
                f"SELECT {COMPANY_SQL} FROM ats_jobs j JOIN ats_companies co "
                "ON co.platform_id=j.platform_id AND co.slug=j.slug WHERE j.slug='direct2.test'").fetchone()[0],
                "Direct")
            conn.execute("UPDATE ats_companies SET company_name='Jobtarget' WHERE slug='agg.test'")
            conn.execute("UPDATE ats_jobs SET raw='{}' WHERE slug='agg.test'")
            check("aggregator senza datore raw resta NULL", conn.execute(
                f"SELECT {COMPANY_SQL} FROM ats_jobs j JOIN ats_companies co "
                "ON co.platform_id=j.platform_id AND co.slug=j.slug WHERE j.slug='agg.test'").fetchone()[0],
                None)
            conn.execute("INSERT INTO ats_jobs(platform_id,slug,raw) VALUES ('francetravail','ft',%s)",
                         (Json({"entreprise": {"nom": "VIVANDA"}}),))
            check("ente pubblico legge entreprise.nom", conn.execute(
                f"SELECT {COMPANY_SQL} FROM ats_jobs j JOIN ats_companies co "
                "ON co.platform_id=j.platform_id AND co.slug=j.slug WHERE j.slug='ft' AND j.raw IS NOT NULL").fetchone()[0],
                "VIVANDA")
            conn.execute("INSERT INTO ats_companies(platform_id,slug,source_type) "
                         "VALUES ('teamtailor','tt','direct')")
            conn.execute("INSERT INTO ats_jobs(platform_id,slug,raw) VALUES ('teamtailor','tt',%s)",
                         (Json({"_jobposting": {"hiringOrganization": {"name": "Team Tailor AB"}}}),))
            check("teamtailor legge _jobposting.hiringOrganization", conn.execute(
                f"SELECT {COMPANY_SQL} FROM ats_jobs j JOIN ats_companies co "
                "ON co.platform_id=j.platform_id AND co.slug=j.slug WHERE j.slug='tt'").fetchone()[0],
                "Team Tailor AB")
    finally:
        with psycopg.connect(ADMIN, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                sql.Identifier(DB)))
    print(f"{len(PASSED)} controlli passati, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
