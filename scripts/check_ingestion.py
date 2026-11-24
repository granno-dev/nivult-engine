#!/usr/bin/env python3
"""Banco Fase 3: migrazioni e ingestione, solo database di prova e HTTP mock."""

from __future__ import annotations

import hashlib
import json
import os
import sys
from unittest import mock as u_mock
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psycopg  # noqa: E402
import httpx  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402

import nivult.config as config  # noqa: E402

ADMIN_DSN = os.environ.get("NIVULT_INGESTION_TEST_ADMIN_URL",
                           "postgresql://127.0.0.1:5432/postgres")
_DSN_ESPLICITO = (os.environ.get("NIVULT_INGESTION_TEST_DATABASE_URL")
                  or os.environ.get("DATABASE_URL"))
_TEMPORANEO = _DSN_ESPLICITO is None
DSN = _DSN_ESPLICITO or make_conninfo(
    ADMIN_DSN, dbname="nivult_fix_ingestion_test")
TEST_DSN = DSN
TEST_DB = config.database_name(DSN)
_host = str(conninfo_to_dict(DSN).get("host") or "")
if not (TEST_DB.endswith(("_test", "_dev"))
        and (_host in ("", "localhost", "127.0.0.1", "::1") or _host.startswith("/"))):
    print("rifiuto: il banco ingestione richiede un database locale _test/_dev")
    raise SystemExit(2)
os.environ["DATABASE_URL"] = DSN
os.environ["MIGRATOR_DATABASE_URL"] = DSN
config.load_dotenv = lambda *args, **kwargs: None

from nivult import migrate  # noqa: E402
from nivult.ingestion import runner, store, sweep as sweep_mod  # noqa: E402
from nivult.ingestion.probe import SOURCES as PROBE_SOURCES  # noqa: E402
from nivult.ingestion.base import Attempt  # noqa: E402
from nivult.ingestion.models import FetchResult, RawJob  # noqa: E402
from nivult.ingestion.sources.arbetsformedlingen import (  # noqa: E402
    ArbetsformedlingenClient, EMPLOYMENT_MAP as AF_EMPLOYMENT)
from nivult.ingestion.sources.fantastic import FantasticClient  # noqa: E402
from nivult.ingestion.sources.france_travail import (  # noqa: E402
    FranceTravailClient, EMPLOYMENT_MAP as FR_EMPLOYMENT)
from nivult.ingestion.sources.indeed import IndeedClient, _query  # noqa: E402
from nivult.ingestion.sources.linkedin import LinkedInClient  # noqa: E402
from nivult.ats.adapters import Join, Vincere  # noqa: E402
from nivult.ats.runner import _spezza_sql as _spezza_sql_ats  # noqa: E402
from nivult.ponte_ats import _come_rawjob  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []
CONN = None


def _drop_database() -> None:
    if not _TEMPORANEO:
        return
    info = conninfo_to_dict(ADMIN_DSN)
    host = str(info.get("host") or "")
    if not (host in ("", "localhost", "127.0.0.1", "::1") or host.startswith("/")):
        raise RuntimeError("il banco ingestione gira solo su PostgreSQL locale")
    if conninfo_to_dict(TEST_DSN).get("dbname") != TEST_DB:
        raise RuntimeError("DSN di test non sicuro")
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                     .format(sql.Identifier(TEST_DB)))


def _crea_database() -> None:
    if not _TEMPORANEO:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute("SELECT 1")
        return
    if conninfo_to_dict(TEST_DSN).get("dbname") != TEST_DB:
        raise RuntimeError("DSN di test non sicuro")
    _drop_database()
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(TEST_DB)))


def require(condition, message="asserzione non soddisfatta"):
    if not condition:
        raise AssertionError(message)


def scenario(name, fn):
    global CONN
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        if CONN is not None:
            CONN.rollback()
        FAILED.append(name)
        print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    else:
        PASSED.append(name)
        print(f"  ok    {name}")


def reset_schema():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT ALL ON SCHEMA public TO CURRENT_USER")


def job(source, sid, posted, **kw):
    url = f"https://mock.example/{source}/{sid}"
    values = dict(
        source=source, source_job_id=sid, url=url, canonical_url=url,
        link_kind="career_site", title=f"Role {sid}",
        title_normalized=f"role {sid}", organization="Acme",
        date_posted=posted, raw={"mock": sid}, countries=["IT"],
        ai_taxonomies_a=["Human Resources"],
    )
    values.update(kw)
    return RawJob(**values)


def make_cluster(*, queries, in_backfill=False, iscritti=1, credit_cap=10000,
                 backfill_cap=2000, cursor=None):
    global CONN
    family, country = "Human Resources", "IT"
    with CONN.cursor() as cur:
        cur.execute("DELETE FROM clusters WHERE family=%s AND country=%s", (family, country))
        cur.execute(
            "INSERT INTO clusters (family,country,daily_credit_cap,daily_request_cap,"
            "backfill_credit_cap,backfill_completed_at) VALUES "
            "(%s,%s,%s,10000,%s,CASE WHEN %s THEN NULL ELSE now() END) RETURNING id",
            (family, country, credit_cap, backfill_cap, in_backfill))
        cid = str(cur.fetchone()[0])
        for source, terms in queries.items():
            for term in terms:
                cur.execute(
                    "INSERT INTO cluster_source_queries(cluster_id,source,query) "
                    "VALUES (%s,%s,%s)", (cid, source, term))
        if cursor:
            cur.execute(
                "INSERT INTO cluster_source_cursors(cluster_id,source,last_seen_posted_at) "
                "VALUES (%s,'indeed',%s)", (cid, cursor))
    CONN.commit()
    with CONN.cursor() as cur:
        found = runner.due_clusters(cur, cid)
    require(len(found) == 1, "cluster non caricato")
    found[0].iscritti = iscritti
    return found[0]


def quota(credits=0, requests=0):
    global CONN
    with CONN.cursor() as cur:
        cur.execute(
            "UPDATE provider_quotas SET monthly_credits_cap=%s, monthly_requests_cap=%s "
            "WHERE provider='indeed'", (credits, requests))
        cur.execute("DELETE FROM provider_budget WHERE provider='indeed'")
    CONN.commit()


def fake_client(responses, *, credits=0, attempts=2, country_codes=("IT",)):
    class FakeClient:
        source = "indeed"
        countries = frozenset(country_codes)
        credits_per_request = credits
        page_size = 100
        max_offset = 10000
        instances = []

        def __init__(self):
            self.attempts = []
            self.calls = []
            self.__class__.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def fetch(self, *, query, country, since, limit, offset=0, **kw):
            self.calls.append((query, offset))
            for _ in range(attempts):
                self.attempts.append(Attempt(200, 1))
            index = sum(1 for q, _ in self.calls if q == query) - 1
            spec = responses[query][index]
            if isinstance(spec, Exception):
                raise spec
            return spec

    return FakeClient


def run_with(cls, cluster, **kw):
    original = runner.CLIENTS
    runner.CLIENTS = (cls,)
    try:
        return runner.ingest_cluster(CONN, cluster, limit=100, max_pages=kw.pop("max_pages", 5),
                                     dry_run=kw.pop("dry_run", False), **kw)
    finally:
        runner.CLIENTS = original


def one(sql, params=()):
    global CONN
    with CONN.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def test_migrations():
    migrations = {m.version: m for m in migrate.discover()}
    require(not migrations[80].transactional, "0080 non riconosciuta no-transaction")
    digest = hashlib.sha256(migrations[80].path.read_bytes()).hexdigest()
    require(digest == "642b7f274c7705ada2ee702eaa9fb14d038def990307109ea67b014996f4098d",
            "0080/checksum modificato")
    require(not migrate.has_transaction_control(
        "CREATE FUNCTION x() RETURNS void LANGUAGE plpgsql AS $$ BEGIN END $$;"),
        "BEGIN PL/pgSQL scambiato per controllo transazione")
    require(migrate.has_transaction_control("BEGIN\nSELECT 1;\nCOMMIT"),
            "COMMIT finale senza ';' non riconosciuto")

    cid = one("SELECT apri_cluster('Human Resources','IT')::text")
    require(one("SELECT count(*) FROM cluster_source_queries WHERE cluster_id=%s AND source='indeed'",
                (cid,)) == 2, "apri_cluster non preserva i due termini")
    require(one("SELECT count(*) FROM cluster_coverage_v WHERE id=%s AND source='indeed'",
                (cid,)) == 1, "coverage non aggregata")
    require(one("SELECT interrogabile AND query LIKE '%%secondo termine%%' "
                "FROM cluster_coverage_v WHERE id=%s AND source='indeed'", (cid,)),
            "termine assente dalla coverage")

    sql = """
    CREATE TABLE IF NOT EXISTS migration_retry_probe(id integer PRIMARY KEY, attempts integer);
    INSERT INTO migration_retry_probe VALUES (1,0) ON CONFLICT (id) DO NOTHING;
    UPDATE migration_retry_probe SET attempts=attempts+1 WHERE id=1;
    DO $$ BEGIN
      IF (SELECT attempts FROM migration_retry_probe WHERE id=1) = 1 THEN
        RAISE EXCEPTION 'fallimento voluto una sola volta';
      END IF;
    END $$;
    INSERT INTO migration_retry_probe VALUES (2,0) ON CONFLICT (id) DO NOTHING;
    """
    m = migrate.Migration(9000, "retry_probe", Path("retry_probe.sql"), sql,
                          "probe", transactional=False)
    with psycopg.connect(DSN, autocommit=True) as conn:
        migrate._execute_no_transaction(conn, m)
        require(conn.execute("SELECT attempts FROM migration_retry_probe WHERE id=1")
                .fetchone()[0] == 2, "retry non eseguito")
        require(conn.execute("SELECT count(*) FROM migration_retry_probe")
                .fetchone()[0] == 2, "retry non idempotente")
        conn.execute("DROP TABLE migration_retry_probe")


def test_multi_termine_e_attempts():
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    cls = fake_client({
        "alpha": [FetchResult([job("indeed", "multi-a", now)], True, 1, 0)],
        "zeta": [FetchResult([job("indeed", "multi-z", now)], True, 1, 0)],
    })
    quota()
    cluster = make_cluster(queries={"indeed": ["zeta", "alpha"]})
    require(cluster.queries["indeed"] == ["alpha", "zeta"], "termini non ordinati")
    totals = run_with(cls, cluster)
    calls = cls.instances[0].calls
    require([q for q, _ in calls] == ["alpha", "zeta"], f"chiamate: {calls}")
    require(totals["richieste"] == 4, f"attempts non contati: {totals}")
    run = one("SELECT id::text || '|' || status || '|' || fetch_complete::text || '|' || "
              "jobs_new::text FROM ingestion_runs WHERE cluster_id=%s", (cluster.id,))
    require(run.endswith("|success|true|2"), run)
    require(one("SELECT count(*) FROM ingestion_runs WHERE cluster_id=%s", (cluster.id,)) == 1,
            "piu' di un run per fonte")
    params = one("SELECT request_params FROM ingestion_runs WHERE cluster_id=%s", (cluster.id,))
    require(params["queries"] == ["alpha", "zeta"], params)
    require(one("SELECT sum(requests) FROM api_usage WHERE cluster_id=%s", (cluster.id,)) == 4,
            "usage non contiene gli attempts")
    require(one("SELECT requests_used FROM provider_budget WHERE provider='indeed'") == 4,
            "provider non conciliato")
    require(one("SELECT requests_made FROM cluster_daily_budget WHERE cluster_id=%s",
                (cluster.id,)) == 4, "richieste cluster non conciliate")
    require(one("SELECT count(*) FROM cluster_source_cursors WHERE cluster_id=%s",
                (cluster.id,)) == 1, "cursore completo assente")


def test_savepoint_e_newest():
    now = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    good1 = job("indeed", "save-good-1", now - timedelta(minutes=2))
    bad = job("indeed", "save-bad", now + timedelta(days=1),
              ai_work_arrangement_office_days=99)
    good2 = job("indeed", "save-good-2", now)
    cls = fake_client({"alpha": [FetchResult([good1, bad, good2], True, 1, 0)]}, attempts=1)
    quota()
    cluster = make_cluster(queries={"indeed": ["alpha"]})
    totals = run_with(cls, cluster)
    require(totals["rifiutate_dal_db"] == 1, totals)
    require(one("SELECT jobs_new FROM ingestion_runs WHERE cluster_id=%s", (cluster.id,)) == 2,
            "contatori includono la riga fallita")
    require(one("SELECT count(*) FROM jobs WHERE source_job_id LIKE 'save-%%'") == 2,
            "savepoint ha annullato altre offerte")
    cursor = one("SELECT last_seen_posted_at FROM cluster_source_cursors WHERE cluster_id=%s",
                 (cluster.id,))
    require(cursor == now, f"newest include il fallimento: {cursor} != {now}")


def test_cursore_non_avanza_se_incompleto():
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    old = now - timedelta(days=2)
    cls = fake_client({
        "alpha": [FetchResult([job("indeed", "partial-a", now)], True, 1, 0)],
        "zeta": [FetchResult([job("indeed", "partial-z", now)], False, 1, 0)],
    }, attempts=1)
    quota()
    cluster = make_cluster(queries={"indeed": ["zeta", "alpha"]}, cursor=old)
    run_with(cls, cluster, max_pages=1)
    require(len(cls.instances[0].calls) == 2, "non tutti i termini sono stati tentati")
    require(not one("SELECT fetch_complete FROM ingestion_runs WHERE cluster_id=%s",
                    (cluster.id,)), "run incompleto marcato completo")
    require(one("SELECT last_seen_posted_at FROM cluster_source_cursors WHERE cluster_id=%s",
                (cluster.id,)) == old, "cursore avanzato su fetch parziale")


def test_chiave_mancante_e_backfill():
    class MissingKey:
        source = "indeed"
        countries = frozenset({"IT"})
        credits_per_request = 0

        def __init__(self):
            raise SystemExit("chiave mock assente")

    quota()
    cluster = make_cluster(queries={"indeed": ["alpha"]}, in_backfill=True)
    run_with(MissingKey, cluster)
    require(one("SELECT count(*) FROM provider_budget WHERE provider='indeed'") == 0,
            "budget consumato prima del client")
    require(one("SELECT count(*) FROM cluster_daily_budget WHERE cluster_id=%s",
                (cluster.id,)) == 0, "budget cluster consumato")
    require(one("SELECT status || '|' || (finished_at IS NOT NULL)::text FROM ingestion_runs "
                "WHERE cluster_id=%s", (cluster.id,)) == "failed|true",
            "run assente o rimasto running")
    require(one("SELECT backfill_completed_at IS NULL FROM clusters WHERE id=%s",
                (cluster.id,)), "backfill completato con fonte saltata")


def test_backfill_con_una_fonte_riuscita_e_una_saltata():
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    good = fake_client({"alpha": [FetchResult([job("indeed", "backfill-good", now)],
                                              True, 1, 0)]}, attempts=1)

    class MissingLinkedIn:
        source = "linkedin"
        countries = frozenset({"IT"})
        credits_per_request = 0

        def __init__(self):
            raise SystemExit("chiave linkedin mock assente")

    quota()
    cluster = make_cluster(queries={"indeed": ["alpha"], "linkedin": ["alpha"]},
                           in_backfill=True)
    original = runner.CLIENTS
    runner.CLIENTS = (good, MissingLinkedIn)
    try:
        runner.ingest_cluster(CONN, cluster, limit=100, dry_run=False, max_pages=5)
    finally:
        runner.CLIENTS = original
    esiti = one("SELECT string_agg(source || ':' || status, ',' ORDER BY source) "
                "FROM ingestion_runs WHERE cluster_id=%s", (cluster.id,))
    require(esiti == "indeed:success,linkedin:failed", esiti)
    require(one("SELECT backfill_completed_at IS NULL FROM clusters WHERE id=%s",
                (cluster.id,)), "backfill chiuso dopo una fonte saltata")


def test_rifiuto_budget_concilia_richiesta():
    cls = fake_client({}, credits=5)
    quota(credits=1000, requests=1000)
    cluster = make_cluster(queries={"indeed": ["alpha"]}, credit_cap=1)
    run_with(cls, cluster)
    require(one("SELECT status FROM ingestion_runs WHERE cluster_id=%s",
                (cluster.id,)) == "aborted_budget", "budget non registrato")
    require(one("SELECT credits_used FROM provider_budget WHERE provider='indeed'") == 0,
            "crediti non rimborsati")
    require(one("SELECT requests_used FROM provider_budget WHERE provider='indeed'") == 0,
            "richiesta non rimborsata")
    require(not cls.instances[0].calls, "fetch chiamata nonostante il rifiuto")


def test_dry_run_non_persiste():
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    cls = fake_client({"alpha": [FetchResult([job("indeed", "dry-free", now)], True, 1, 0)]})
    quota()
    cluster = make_cluster(queries={"indeed": ["alpha"]}, in_backfill=True)
    totals = run_with(cls, cluster, dry_run=True)
    require(totals["richieste"] == 2, totals)
    for table in ("ingestion_runs", "api_usage", "cluster_daily_budget", "cluster_source_cursors"):
        require(one(f"SELECT count(*) FROM {table} WHERE cluster_id=%s", (cluster.id,)) == 0,
                f"dry-run ha scritto {table}")
    require(one("SELECT count(*) FROM jobs WHERE source_job_id='dry-free'") == 0,
            "dry-run ha scritto jobs")
    require(one("SELECT count(*) FROM provider_budget WHERE provider='indeed'") == 0,
            "dry-run ha scritto provider_budget")

    paid = fake_client({"alpha": [FetchResult([job("indeed", "dry-paid", now)], True, 1, 3)]},
                       credits=1)
    quota(credits=100)
    cluster = make_cluster(queries={"indeed": ["alpha"]}, in_backfill=True)
    skipped = run_with(paid, cluster, dry_run=True)
    require(skipped["richieste"] == 0 and not paid.instances, "paid senza autorizzazione")
    allowed = run_with(paid, cluster, dry_run=True, allow_paid=True)
    require(allowed["crediti"] == 3 and allowed["richieste"] == 2, allowed)
    require(one("SELECT count(*) FROM provider_budget WHERE provider='indeed'") == 0,
            "paid dry-run ha consumato budget locale")
    require(one("SELECT count(*) FROM ingestion_runs WHERE cluster_id=%s", (cluster.id,)) == 0,
            "paid dry-run ha aperto un run")


def test_upsert_salario_atomico():
    now = datetime.now(timezone.utc)
    rich = job("indeed", "salary-rich", now, salary_min=40000, salary_max=50000,
               salary_currency="EUR", salary_period="year", salary_da="dichiarato")
    poor = job("indeed", "salary-rich", now)
    newer = job("indeed", "salary-rich", now, salary_min=60000,
                salary_currency="EUR", salary_period="year", salary_da="dichiarato")
    with CONN.cursor() as cur:
        jid, is_new = store.upsert_job(cur, rich)
        require(is_new, "inserimento salario non nuovo")
        store.upsert_job(cur, poor)
    CONN.commit()
    row = one("SELECT salary_min::text || '|' || salary_max::text || '|' || salary_currency "
              "FROM jobs WHERE id=%s", (jid,))
    require(row == "40000|50000|EUR", f"salario ricco cancellato: {row}")
    with CONN.cursor() as cur:
        store.upsert_job(cur, newer)
    CONN.commit()
    with CONN.cursor() as cur:
        cur.execute("SELECT salary_min,salary_max,salary FROM jobs WHERE id=%s", (jid,))
        mn, mx, amount = cur.fetchone()
    require(mn == 60000 and mx is None, "estremi mescolati fra payload")
    require(amount["value"] == {"minValue": 60000, "unitText": "YEAR"}, amount)
    require(amount["@type"] == "MonetaryAmount", amount)

    cross = job("linkedin", "salary-cross-poor", now)
    cross.url = rich.url
    cross.canonical_url = rich.canonical_url
    with CONN.cursor() as cur:
        cross_id, inserted = store.upsert_job(cur, cross)
    CONN.commit()
    require(cross_id == jid and not inserted, "conflitto canonical_url non percorso")
    require(one("SELECT salary_min FROM jobs WHERE id=%s", (jid,)) == 60000,
            "conflitto canonical_url ha cancellato il salario")


def test_date_posted_e_rilevazioni():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    vecchia_ultima = now - timedelta(days=5)
    primo = job("indeed", "date-proof", now, date_posted_declared=True)
    with CONN.cursor() as cur:
        jid, is_new = store.upsert_job(cur, primo)
        require(is_new, "inserimento data dichiarata")
        cur.execute("UPDATE jobs SET first_seen_at = %s, last_seen_alive_at = %s "
                    "WHERE id = %s", (vecchia_ultima, vecchia_ultima, jid))
    CONN.commit()
    require(one("SELECT date_posted_declared FROM jobs WHERE id=%s", (jid,)) is True,
            "data dichiarata non persistita")
    prima = one("SELECT first_seen_at FROM jobs WHERE id=%s", (jid,))

    nuova_data = now + timedelta(hours=1)
    fallback = job("indeed", "date-proof", nuova_data, date_posted_declared=False)
    with CONN.cursor() as cur:
        store.upsert_job(cur, fallback)
    CONN.commit()
    with CONN.cursor() as cur:
        cur.execute("SELECT first_seen_at, last_seen_alive_at, date_posted_declared, "
                    "date_posted FROM jobs WHERE id=%s", (jid,))
        prima_dopo, ultima, dichiarata, posted = cur.fetchone()
    require(prima_dopo == prima, "first_seen_at riscritto dall'upsert")
    require(ultima > vecchia_ultima, "last_seen_alive_at non aggiornato")
    require(dichiarata is False, "fallback marcato come data dichiarata")
    require(posted == nuova_data, "date_posted non propagato senza cambiare lo store")

    ignota = job("indeed", "date-unknown", now)
    with CONN.cursor() as cur:
        unknown_id, _ = store.upsert_job(cur, ignota)
    CONN.commit()
    require(one("SELECT date_posted_declared FROM jobs WHERE id=%s", (unknown_id,)) is None,
            "provenienza ignota non rimasta NULL")


def test_contratto_e_ponte():
    base = job("indeed", "contract", datetime.now(timezone.utc), ai_employment_type="full-time")
    require(base.ai_employment_type == "FULL_TIME", base.ai_employment_type)
    require(base.date_posted_declared is None, "provenienza ignota resa vera per default")
    unknown = job("indeed", "contract-unknown", datetime.now(timezone.utc),
                  ai_employment_type="indefinito-non-noto")
    require(unknown.ai_employment_type is None, "valore ignoto inventato")
    riga_ats = {
        "id": "ats-1", "title": "HR", "url": "https://acme.example/jobs/1",
        "country": "IT", "city": "Milano", "posted_at": datetime.now(timezone.utc),
        "date_posted_declared": True,
        "salary_min": Decimal("42000"), "salary_max": Decimal("48000"),
        "salary_currency": "EUR", "salary_period": "year", "salary_da": "dichiarato",
        "raw": {}, "seniority": "mid", "remote": "hybrid", "skills": [],
        "lang": "it", "family": "Human Resources", "company_name": "Acme",
        "site_domain": "acme.example", "company_logo": None,
        "valid_through": datetime(2026, 11, 15).date(),
    }
    ats = _come_rawjob(riga_ats)
    require(ats.salary["@type"] == "MonetaryAmount", ats.salary)
    require(ats.salary["value"]["minValue"] == 42000, ats.salary)
    require("min_value" not in ats.salary and "max_value" not in ats.salary, ats.salary)
    require(ats.date_posted_declared is True, "data ATS dichiarata persa nel ponte")
    require(ats.date_valid_through == datetime(2026, 11, 15, tzinfo=timezone.utc),
            "valid_through ATS dichiarato non propagato")
    senza_scadenza = _come_rawjob({k: v for k, v in riga_ats.items()
                                   if k != "valid_through"})
    require(senza_scadenza.date_valid_through is None,
            "scadenza assente inventata dal ponte")
    fallback = _come_rawjob({**riga_ats, "id": "ats-2",
                             "url": "https://acme.example/jobs/2",
                             "date_posted_declared": False})
    require(fallback.date_posted_declared is False,
            "stima/fallback ATS marcato come pubblicazione reale")
    senza_prova = _come_rawjob({k: v for k, v in riga_ats.items()
                                if k != "date_posted_declared"})
    require(senza_prova.date_posted_declared is None,
            "assenza di prova non conservata come NULL")
    rtx = _come_rawjob({**riga_ats, "country": "IT", "raw": {"description":
        "Date Posted: 2026-10-30 Country: United States of America "
        "Location: US-NY-ROME-104 Position Role Type: Onsite"}})
    require(rtx.countries == ["US"],
            "i metadati RTX devono correggere la città omonima geocodificata")


def mock_http(client, handler):
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    client.bucket.take = lambda n=1: None
    client._backoff = lambda attempt: 0
    if hasattr(client, "_piano"):
        client._piano = lambda: None


def test_indeed_filtro_e_salario():
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=90)
    seen = []

    def fresh(key, when):
        return {"key": key, "title": "HR", "sourceEmployerName": "Acme",
                "dateOnIndeed": int(when.timestamp() * 1000),
                "description": {"html": "<p>Descrizione</p>"},
                "location": {"city": "Milano"}, "attributes": [{"key": "CF3CP"}],
                "compensation": {"baseSalary": {"unitOfWork": "HOUR", "range": [20, 25]},
                                 "currencyCode": "EUR"}}

    payload = {"data": {"jobSearch": {"pageInfo": {"nextCursor": None},
               "results": [{"job": fresh("fresh", now - timedelta(minutes=30))},
                           {"job": fresh("old", now - timedelta(hours=3))}]}}}

    def handler(request):
        seen.append(json.loads(request.content)["query"])
        return httpx.Response(200, json=payload)

    client = IndeedClient()
    mock_http(client, handler)
    result = client.fetch(query="HR", country="IT", since=since, limit=25)
    client.close()
    require('field: "dateOnIndeed"' in seen[0] and 'start: "2h"' in seen[0], seen[0])
    require(len(result.jobs) == 1 and result.out_of_window == 1 and result.received == 2,
            "filtro locale o conteggio ricevuti")
    require(result.requests_made == 1, result.requests_made)
    require(result.jobs[0].ai_employment_type == "FULL_TIME", "employment Indeed")
    require(result.jobs[0].salary["value"] == {"minValue": 20, "maxValue": 25,
                                               "unitText": "HOUR"}, result.jobs[0].salary)
    scalar = dict(fresh("scalar", now), compensation={"baseSalary": 30, "currencyCode": "EUR"})
    parsed = client._to_raw_job(scalar, "IT", "it")
    require(parsed.salary_min == 30 and parsed.salary_max == 30, "range scalare Indeed")
    query = _query("HR", "Italia", 25, None, since)
    require('filters: [{ date: { field: "dateOnIndeed", start: "2h" }}]' in query, query)


def test_attempts_france_e_linkedin():
    search_calls = [0]

    def fr_handler(request):
        if request.url.host == "entreprise.francetravail.fr":
            return httpx.Response(200, json={"access_token": "mock", "expires_in": 1200})
        search_calls[0] += 1
        if search_calls[0] == 1:
            return httpx.Response(500, json={"error": "mock retry"})
        return httpx.Response(200, json={"resultats": []},
                              headers={"Content-Range": "offres 0-149/0"})

    fr = FranceTravailClient(client_id="mock", client_secret="mock", max_retries=2)
    mock_http(fr, fr_handler)
    result = fr.fetch(query="RH", country="FR", since=datetime.now(timezone.utc))
    fr.close()
    require(result.requests_made == 3, f"token/retry France: {result.requests_made}")

    html = """<li><div class='job-search-card' data-entity-urn='urn:li:jobPosting:123'>
      <a class='base-card__full-link' href='https://www.linkedin.com/jobs/view/123?trk=x'></a>
      <h3 class='base-search-card__title'>HR Manager</h3>
      <h4 class='base-search-card__subtitle'>Acme</h4>
      <span class='job-search-card__location'>Milano, Italia</span>
      <time datetime='2026-10-23'></time></div></li>"""
    detail = """<div class='show-more-less-html__markup'><p>Requisiti veri</p></div>
      <h3 class='description__job-criteria-subheader'>Employment type</h3>
      <span class='description__job-criteria-text'>Full-time</span>"""

    def li_handler(request):
        if "seeMoreJobPostings" in str(request.url):
            return httpx.Response(200, text=html)
        return httpx.Response(200, text=detail)

    li = LinkedInClient()
    mock_http(li, li_handler)
    result = li.fetch(query="HR", country="IT", since=datetime.now(timezone.utc))
    li.close()
    require(result.requests_made == 2, f"dettaglio LinkedIn non contato: {result.requests_made}")
    require(result.jobs[0].ai_employment_type == "FULL_TIME", "employment LinkedIn")
    require("Requisiti veri" in result.jobs[0].raw["descrizione"], "dettaglio LinkedIn")


def test_fantastic_iso_e_vincere():
    fantastic = FantasticClient(api_key="mock")
    parsed = fantastic._to_raw_job({
        "id": 1, "url": "https://acme.example/jobs/fantastic-1", "title": "HR",
        "date_posted": "2026-10-23T10:00:00", "countries_derived": ["Germany"],
        "ai_employment_type": ["full-time"],
        "salary": {"value": {"minValue": 50000, "maxValue": 60000, "unitText": "YEAR"}},
    })
    fantastic.close()
    require(parsed.countries == ["DE"], parsed.countries)
    require(parsed.salary_currency == "EUR" and parsed.salary["currency"] == "EUR",
            "parser salariale non usa ISO")
    require(parsed.ai_employment_type == "FULL_TIME", parsed.ai_employment_type)
    require(parsed.date_posted_declared is True,
            "la data fornita dalla fonte non risulta dichiarata")

    def article(jid):
        return f"""<article class="job"><a href="https://acme.vincere.io/careers/job/{jid}/role/milano">
          <h3>Role {jid}</h3></a><div class="job__location">Milano</div>
          <div class="job__type">Full time</div>Published date:
          <div class="col-md-10">23-10-2026</div></article>"""

    def handler(request):
        page = request.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(200, text=article(1))
        if page == "2":
            return httpx.Response(200, text=article(1) + article(2))
        return httpx.Response(200, text="<html></html>")

    vincere = Vincere()
    vincere.client.close()
    vincere.client = httpx.Client(transport=httpx.MockTransport(handler))
    jobs = vincere.jobs("acme")
    vincere.close()
    require([j.external_id for j in jobs] == ["1", "2"], "Vincere due pagine/duplicati")


def test_join_page_data():
    for chiave in ("initialState", "pageData"):
        jobs = {
            "items": [{
                "idParam": "abc",
                "title": "HR Manager",
                "createdAt": "2026-10-01T10:00:00Z",
                "city": {"cityName": "Milano"},
                "country": {"iso3166": "IT"},
            }],
            "pagination": {"page": 1, "pageCount": 1, "pageSize": 5,
                           "perPage": 5, "total": 1},
        }
        payload = {"props": {"pageProps": {chiave: {"jobs": jobs}}}}
        testo = ('<script id="__NEXT_DATA__" type="application/json">'
                 + json.dumps(payload) + "</script>")
        adapter = Join()
        adapter.client = u_mock.Mock()
        adapter.client.get.return_value = u_mock.Mock(status_code=200, text=testo)
        offerte = adapter.jobs("acme")
        require(len(offerte) == 1 and offerte[0].external_id == "abc")
        require(offerte[0].url == "https://join.com/companies/acme/abc")


def test_indice_ricerca_ats():
    schema = (ROOT / "src" / "nivult" / "ats" / "schema.sql").read_text()
    istruzioni = _spezza_sql_ats(schema)
    create = next(s for s in istruzioni
                  if s.startswith("CREATE INDEX IF NOT EXISTS ats_jobs_search_idx"))
    alter = next(s for s in istruzioni
                 if s.startswith("ALTER INDEX IF EXISTS ats_jobs_search_idx"))
    with CONN.cursor() as cur:
        cur.execute("CREATE TEMP TABLE ats_jobs (title text, raw jsonb) ON COMMIT DROP")
        cur.execute(create)
        cur.execute(alter)
        cur.execute("SELECT reloptions FROM pg_class WHERE oid = "
                    "to_regclass('ats_jobs_search_idx')")
        opzioni = cur.fetchone()[0] or []
        require(any(v.lower() in ("fastupdate=off", "fastupdate=false", "fastupdate=0")
                    for v in opzioni), opzioni)
        cur.execute("ALTER INDEX ats_jobs_search_idx SET (fastupdate = true)")
        cur.execute(alter)
        cur.execute("SELECT reloptions FROM pg_class WHERE oid = "
                    "to_regclass('ats_jobs_search_idx')")
        require(any(v.lower() in ("fastupdate=off", "fastupdate=false", "fastupdate=0")
                    for v in (cur.fetchone()[0] or [])))
        cur.execute("INSERT INTO ats_jobs VALUES (%s, %s::jsonb)",
                    ("HR Manager", '{"description":"People operations"}'))
        cur.execute("SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                    "WHERE indexrelid = to_regclass('ats_jobs_search_idx')")
        definizione = cur.fetchone()[0].split(" WITH (", 1)[0]
        espressione = definizione.split(" USING gin (", 1)[1].rsplit(")", 1)[0]
        cur.execute("SELECT title FROM ats_jobs WHERE " + espressione
                    + " @@ plainto_tsquery('simple', 'people')")
        require(cur.fetchone()[0] == "HR Manager")
    CONN.commit()


def test_tecnologie_ricerca_normalizzata():
    schema = (ROOT / "src" / "nivult" / "ats" / "schema.sql").read_text()
    istruzioni = _spezza_sql_ats(schema)

    def ddl(inizio):
        return next(s for s in istruzioni if s.startswith(inizio))

    primo = "00000000-0000-0000-0000-000000000001"
    secondo = "00000000-0000-0000-0000-000000000002"
    posted_primo = datetime(2026, 10, 1, tzinfo=timezone.utc)
    posted_secondo = datetime(2026, 10, 2, tzinfo=timezone.utc)
    with CONN.cursor() as cur:
        cur.execute("SET LOCAL search_path TO pg_temp, public")
        cur.execute("CREATE TEMP TABLE ats_jobs ("
                    "id uuid PRIMARY KEY, platform_id text, country text, "
                    "seniority text, remote text, posted_at timestamptz, "
                    "expired_at timestamptz) ON COMMIT DROP")
        cur.execute("CREATE TEMP TABLE tecnologie_v1 ("
                    "job_id uuid PRIMARY KEY, tecnologie jsonb NOT NULL) ON COMMIT DROP")
        for inizio in (
                "CREATE TABLE IF NOT EXISTS tecnologie_ricerca",
                "CREATE OR REPLACE FUNCTION tecnologie_ricerca_rigenera_job",
                "CREATE OR REPLACE FUNCTION tecnologie_ricerca_sincronizza_v1",
                "CREATE OR REPLACE FUNCTION tecnologie_ricerca_aggiorna_posted_at",
                "DROP TRIGGER IF EXISTS trg_tecnologie_ricerca_v1",
                "CREATE TRIGGER trg_tecnologie_ricerca_v1",
                "DROP TRIGGER IF EXISTS trg_tecnologie_ricerca_posted_at",
                "CREATE TRIGGER trg_tecnologie_ricerca_posted_at",
                "CREATE INDEX IF NOT EXISTS tecnologie_ricerca_ordine_idx",
                "CREATE INDEX IF NOT EXISTS ats_jobs_attive_platform_country_v2_idx",
                "CREATE INDEX IF NOT EXISTS ats_jobs_attive_country_v2_idx",
                "CREATE INDEX IF NOT EXISTS ats_jobs_attive_platform_country_senior_remote_idx"):
            istruzione = ddl(inizio)
            if inizio.startswith("CREATE OR REPLACE FUNCTION tecnologie_ricerca_sincronizza_v1"):
                istruzione = istruzione.replace(
                    "PERFORM tecnologie_ricerca_rigenera_job",
                    "PERFORM pg_temp.tecnologie_ricerca_rigenera_job")
            if inizio.startswith("CREATE TRIGGER trg_tecnologie_ricerca"):
                istruzione = istruzione.replace(
                    "EXECUTE FUNCTION tecnologie_ricerca_",
                    "EXECUTE FUNCTION pg_temp.tecnologie_ricerca_")
            cur.execute(istruzione)
        cur.execute("INSERT INTO ats_jobs VALUES "
                    "(%s, 'workday', 'US', 'senior', 'remote', %s, NULL), "
                    "(%s, 'greenhouse', 'DE', 'senior', 'remote', %s, NULL)",
                    (primo, posted_primo, secondo, posted_secondo))
        cur.execute("INSERT INTO tecnologie_v1 VALUES (%s, %s::jsonb), (%s, %s::jsonb)",
                    (primo, '["SAP", "sap", "Snowflake", "SAP"]',
                     secondo, '["SAPUI5"]'))

        cur.execute("SELECT tecnologia, posted_at FROM tecnologie_ricerca "
                    "WHERE job_id=%s ORDER BY tecnologia", (primo,))
        require(cur.fetchall() == [("sap", posted_primo), ("snowflake", posted_primo)],
                "normalizzazione o deduplica SAP/sap")

        def ids(technology):
            cur.execute("SELECT job_id::text FROM tecnologie_ricerca "
                        "WHERE tecnologia=lower(%s)", (technology,))
            return {r[0] for r in cur.fetchall()}

        for technology in ("SAP", "sap", "sAp"):
            require(ids(technology) == {primo}, technology)
        require(ids("SAPUI5") == {secondo}, "SAPUI5 confuso con SAP")
        require(ids("SAPUI") == set(), "match parziale SAPUI")
        require(ids("non-esiste") == set(), "tecnologia inesistente")

        cur.execute("UPDATE tecnologie_v1 SET tecnologie=%s::jsonb WHERE job_id=%s",
                    ('["SAP", "sap"]', primo))
        cur.execute("SELECT tecnologia FROM tecnologie_ricerca WHERE job_id=%s", (primo,))
        require(cur.fetchall() == [("sap",)], "update tecnologie non rigenerato")
        require(ids("SAPUI5") == {secondo}, "update ha rigenerato un altro job")

        nuovo_posted = datetime(2026, 10, 30, tzinfo=timezone.utc)
        cur.execute("UPDATE ats_jobs SET posted_at=%s WHERE id=%s", (nuovo_posted, primo))
        cur.execute("SELECT posted_at FROM tecnologie_ricerca WHERE job_id=%s", (primo,))
        require(cur.fetchall() == [(nuovo_posted,)], "posted_at non propagato")

        cur.execute("DELETE FROM tecnologie_v1 WHERE job_id=%s", (secondo,))
        require(ids("SAPUI5") == set(), "delete tecnologie non propagato")
        require(ids("SAP") == {primo}, "delete ha cancellato un altro job")

        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("EXPLAIN SELECT job_id FROM tecnologie_ricerca "
                    "WHERE tecnologia='sap' "
                    "ORDER BY posted_at DESC NULLS LAST, job_id LIMIT 1")
        piano = "\n".join(r[0] for r in cur.fetchall())
        require("tecnologie_ricerca_ordine_idx" in piano, piano)

        for nome, colonne in (
                ("ats_jobs_attive_platform_country_v2_idx",
                 "(platform_id, country, posted_at DESC NULLS LAST, id)"),
                ("ats_jobs_attive_country_v2_idx",
                 "(country, posted_at DESC NULLS LAST, id)"),
                ("ats_jobs_attive_platform_country_senior_remote_idx",
                 "(platform_id, country, seniority, remote, posted_at DESC NULLS LAST, id)")):
            cur.execute("SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                        "WHERE indexrelid=to_regclass(%s)", (nome,))
            riga = cur.fetchone()
            require(riga is not None, f"indice assente: {nome}")
            require(colonne in riga[0] and "WHERE (expired_at IS NULL)" in riga[0], riga[0])
        cur.execute("DROP TABLE tecnologie_ricerca")
    CONN.commit()


def test_mappe_nazionali():
    require(ArbetsformedlingenClient.countries == frozenset({"SE"}))
    require(AF_EMPLOYMENT["Tidsbegränsad anställning"] == "TEMPORARY")
    require(FR_EMPLOYMENT["CDD"] == "TEMPORARY")
    require(FR_EMPLOYMENT["LIB"] == "CONTRACTOR")


def test_fonte_ritirata():
    require("fantastic" not in PROBE_SOURCES,
            "il probe manuale non deve offrire la fonte pensionata")

    class FonteAttiva:
        def __init__(self):
            self.chiamate = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def fetch_removals(self, since):
            self.chiamate.append(since)
            return [], 0

    with u_mock.patch(
            "nivult.ingestion.sources.fantastic.FantasticClient",
            side_effect=AssertionError("la fonte pensionata e' stata chiamata")), \
         u_mock.patch(
            "nivult.ingestion.sources.arbetsformedlingen.ArbetsformedlingenClient",
            FonteAttiva):
        conn = u_mock.MagicMock()
        out = sweep_mod.declared_removals(conn, dry_run=True)
    require(out == {"fantastic": 0, "arbetsformedlingen": 0}, out)
    conn.cursor.assert_not_called()


def test_skills_rest_e_serializer():
    from nivult.ats import profilo
    from nivult.ats.esporta import _skills_pulite
    from nivult.ats.pagine_seo import _riga_feed

    with u_mock.patch.object(profilo, "ESCO_ATTIVO", False):
        for testo in ("Restauración", "restaurant", "resté", "restà",
                      "rest of", "rest", "Rest", "Rest API"):
            skills = profilo.deterministico(testo, "", {})[2]
            require("rest" not in skills, (testo, skills))
        for testo in ("REST", "REST API", "RESTful"):
            skills = profilo.deterministico(testo, "", {})[2]
            require("rest" in skills, (testo, skills))
        require("java" not in profilo.deterministico("Javà", "", {})[2])
        require("c++" in profilo.deterministico("C++", "", {})[2])

    with u_mock.patch.object(profilo, "ESCO_ATTIVO", True), \
         u_mock.patch("nivult.ats.esco.estrai", return_value=["English", "PYTHON"]):
        skills = profilo.deterministico("english python", "", {})[2]
        require(len({s.casefold() for s in skills}) == len(skills), skills)

    require(_skills_pulite(["Python", "python", "English", "english"]) == ["python"])
    now = datetime.now(timezone.utc)
    esempio = {
        "title": "Key Account Manager - Restauración", "company": "Acme",
        "company_slug": "acme", "country": "ES", "city": "Madrid",
        "location": "Madrid", "url": "https://example.com/1", "remote": None,
        "seniority": None, "skills": ["rest", "Python", "python", "English"],
        "salary_min": None, "salary_max": None, "salary_currency": None,
        "posted_at": now, "created_at": now, "fetched_at": now,
    }
    require(_riga_feed("workday", esempio)["skills"] == ["python"])
    esempio["title"] = "REST API Engineer"
    esempio["skills"] = ["REST", "Python", "python"]
    require(_riga_feed("workday", esempio)["skills"] == ["REST", "python"])


def _main() -> int:
    global CONN
    _crea_database()
    print(f"verifica ingestione — {config.safe_dsn(DSN)} (HTTP solo mock)")
    reset_schema()
    if migrate.main(["up", "--target", "80"]) != 0:
        return 1
    with psycopg.connect(DSN) as conn:
        conn.execute("INSERT INTO clusters(family,country) VALUES ('Human Resources','IT')")
        cid = conn.execute("SELECT id FROM clusters WHERE family='Human Resources' AND country='IT'") \
            .fetchone()[0]
        conn.execute("INSERT INTO cluster_source_queries(cluster_id,source,query) "
                     "VALUES (%s,'indeed','Human Resources'),(%s,'indeed','secondo termine')",
                     (cid, cid))
        conn.commit()
    if migrate.main(["up", "--target", "81"]) != 0:
        return 1
    if migrate.main(["up", "--target", "92"]) != 0:
        return 1

    tests = [
        ("fonte pensionata: nessuna chiamata da sweep o probe", test_fonte_ritirata),
        ("migrazioni, PK3, coverage e retry", test_migrations),
        ("multi-termine, un run e attempts reali", test_multi_termine_e_attempts),
        ("savepoint per offerta e newest", test_savepoint_e_newest),
        ("cursore fermo se un termine e' incompleto", test_cursore_non_avanza_se_incompleto),
        ("chiave mancante prima di budget e backfill aperto", test_chiave_mancante_e_backfill),
        ("backfill incompleto con una fonte riuscita e una saltata",
         test_backfill_con_una_fonte_riuscita_e_una_saltata),
        ("rifiuto cluster concilia richiesta provider", test_rifiuto_budget_concilia_richiesta),
        ("dry-run senza persistenza e paid esplicito", test_dry_run_non_persiste),
        ("upsert salario atomico e non distruttivo", test_upsert_salario_atomico),
        ("date dichiarate, fallback e rilevazioni preservate", test_date_posted_e_rilevazioni),
        ("employment centrale e MonetaryAmount ponte ATS", test_contratto_e_ponte),
        ("Indeed dateOnIndeed, filtro locale e range", test_indeed_filtro_e_salario),
        ("attempts token/retry France e dettaglio LinkedIn", test_attempts_france_e_linkedin),
        ("Fantastic ISO e Vincere due pagine", test_fantastic_iso_e_vincere),
        ("Join pageData e initialState", test_join_page_data),
        ("mappe nazionali importabili senza rete", test_mappe_nazionali),
        ("indice GIN ATS valido e senza fast update", test_indice_ricerca_ats),
        ("tecnologie normalizzate, trigger e indici", test_tecnologie_ricerca_normalizzata),
        ("REST Unicode, dedup skills e serializer SEO", test_skills_rest_e_serializer),
    ]
    CONN = psycopg.connect(DSN)
    try:
        for name, fn in tests:
            print(f"\n{name}")
            scenario(name, fn)
    finally:
        CONN.rollback()
        CONN.close()
        CONN = None

    print(f"\n{len(PASSED)} superati, {len(FAILED)} falliti")
    return 1 if FAILED else 0


def main() -> int:
    try:
        return _main()
    finally:
        _drop_database()


if __name__ == "__main__":
    raise SystemExit(main())
