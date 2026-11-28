import argparse
import os
from datetime import UTC, datetime
from types import SimpleNamespace

import psycopg
import pytest

from nivult import migrate
from nivult.ats import backfill_wave2, backlog_wave2
from nivult.ats.backfill_wave2 import esegui as backfill
from nivult.ats.backfill_wave2 import main as backfill_main
from nivult.ats.esporta import flusso
from nivult.ats.mantenimento import expira

DSN = os.environ.get("NIVULT_TEST_ATS_DSN")
EMPTY_DSN = os.environ.get("NIVULT_TEST_EMPTY_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="richiede PostgreSQL ATS isolato esplicito")


@pytest.fixture
def db():
    with psycopg.connect(DSN, autocommit=True) as conn:
        _clean(conn)
        yield conn
        _clean(conn)


def _clean(conn):
    conn.execute(
        "TRUNCATE ats_jobs, ats_companies, ats_platforms, "
        "ats_delta_hash, ats_delta_state CASCADE"
    )
    conn.execute(
        "INSERT INTO ats_platforms (id, name, api_type) VALUES ('oracle', 'Oracle', 'json')"
    )


def _company(conn, slug):
    conn.execute(
        "INSERT INTO ats_companies (platform_id, slug, last_ok_at, lettura_parziale) "
        "VALUES ('oracle', %s, now(), false)",
        (slug,),
    )


def _seed_jobs(conn, slug, count, *, zombie=False, absent=False):
    conn.execute(
        f"""
        INSERT INTO ats_jobs (platform_id, slug, external_id, title, url, posted_at,
                              created_at, content_updated_at, active_since)
        SELECT 'oracle', %s, %s || '-' || g, 'Role', 'https://example.com/' || g, now(),
               now() - interval '2 days', now() - interval '2 days',
               CASE WHEN %s THEN now() - interval '400 days'
                    ELSE now() - interval '30 days' END
          FROM generate_series(1, {int(count)}) g
        """,
        (slug, slug, zombie),
    )
    if absent:
        conn.execute(
            "UPDATE ats_jobs SET fetched_at = now() - interval '8 days' WHERE slug = %s",
            (slug,),
        )
        conn.execute(
            "UPDATE ats_jobs SET absent_since = now() - interval '8 days', absence_count = 2 "
            "WHERE slug = %s",
            (slug,),
        )


def test_migrations_e_indici_sono_validi(db):
    versions = {
        r[0] for r in db.execute("SELECT version FROM schema_migrations").fetchall()
    }
    assert {101, 102} <= versions
    for name in (
        "ats_jobs_assenze_idx",
        "ats_jobs_zombie_idx",
        "ats_jobs_content_updated_idx",
    ):
        assert db.execute(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(%s)",
            (f"public.{name}",),
        ).fetchone()[0] is True


def test_require_table_salta_db_non_ats_senza_registrare(monkeypatch):
    if not EMPTY_DSN:
        pytest.skip("serve NIVULT_TEST_EMPTY_DSN")
    migrations = [m for m in migrate.discover() if m.version in (93, 94, 97, 101, 102)]
    assert all(m.required_table for m in migrations)
    monkeypatch.setattr(migrate, "discover", lambda: migrations)
    monkeypatch.setattr(migrate, "migrator_database_url", lambda: EMPTY_DSN)

    assert migrate.cmd_up(argparse.Namespace(target=None, dry_run=False)) == 0
    with psycopg.connect(EMPTY_DSN) as conn:
        assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] == 0
        assert conn.execute("SELECT to_regclass('public.ats_jobs')").fetchone()[0] is None


def test_recovery_indice_artificialmente_invalido(db):
    db.execute(
        "UPDATE pg_index SET indisvalid = false "
        "WHERE indexrelid = to_regclass('public.ats_jobs_assenze_idx')"
    )
    migration = next(m for m in migrate.discover() if m.version == 102)
    with psycopg.connect(DSN, autocommit=True) as conn:
        migrate._execute_no_transaction(conn, migration)
    assert db.execute(
        "SELECT indisvalid FROM pg_index WHERE indexrelid = "
        "to_regclass('public.ats_jobs_assenze_idx')"
    ).fetchone()[0] is True


class NonClosingConnection:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def __getattr__(self, name):
        return getattr(self.conn, name)


def test_backfill_cli_non_inonda_delta_e_update_riprende_post_commit(
    db, tmp_path, monkeypatch
):
    _company(db, "backfill")
    job_id = db.execute(
        """
        INSERT INTO ats_jobs (platform_id, slug, external_id, title, url, posted_at,
                              created_at, fetched_at, content_updated_at, active_since)
        VALUES ('oracle', 'backfill', '1', 'Role', 'https://example.com/1', now(),
                now() - interval '2 days', now() - interval '2 days',
                '2026-11-01T00:00:00+00:00', now())
        RETURNING id
        """
    ).fetchone()[0]
    db.execute("ALTER TABLE ats_jobs DISABLE TRIGGER trg_riempi_posted_at")
    db.execute("ALTER TABLE ats_jobs DROP CONSTRAINT ats_jobs_posted_at_minima_check")
    db.execute(
        "UPDATE ats_jobs SET posted_at = '0202-03-19T15:12:12+00:00', "
        "active_since = NULL WHERE id = %s",
        (job_id,),
    )
    db.execute("ALTER TABLE ats_jobs ENABLE TRIGGER trg_riempi_posted_at")
    db.execute(
        "UPDATE ats_jobs SET content_updated_at = '2026-11-01T00:00:00+00:00' "
        "WHERE id = %s",
        (job_id,),
    )
    db.execute(
        "ALTER TABLE ats_jobs ADD CONSTRAINT ats_jobs_posted_at_minima_check "
        "CHECK (posted_at IS NULL OR posted_at >= TIMESTAMPTZ '2020-01-01 00:00:00+00') "
        "NOT VALID"
    )
    db.execute(
        "INSERT INTO ats_delta_hash (job_id, content_hash) VALUES (%s, 'baseline')",
        (job_id,),
    )
    db.execute(
        "INSERT INTO ats_delta_state (chiave, valore) VALUES ('flusso', now())"
    )
    expected = datetime(2026, 11, 1, tzinfo=UTC)
    monkeypatch.setattr("nivult.ats.esporta.CARTELLA", str(tmp_path / "exports"))

    with psycopg.connect(DSN) as raw:
        wrapper = NonClosingConnection(raw)
        monkeypatch.setattr(
            backfill_wave2, "psycopg", SimpleNamespace(connect=lambda dsn: wrapper)
        )
        assert backfill_main([
            "--dsn", DSN, "--apply", "--steps", "AB", "--batch-size", "1", "--sleep-ms", "0"
        ]) == 0
        assert raw.execute(
            "SELECT current_setting('nivult.skip_content_updated', true)"
        ).fetchone()[0] == ""
        assert raw.execute(
            "SELECT content_updated_at FROM ats_jobs WHERE id = %s", (job_id,)
        ).fetchone()[0] == expected
        assert raw.execute(
            "SELECT active_since IS NOT NULL FROM ats_jobs WHERE id = %s", (job_id,)
        ).fetchone()[0] is True

    assert flusso(DSN)["aggiornate"] == 0
    with psycopg.connect(DSN) as raw:
        raw.execute("UPDATE ats_jobs SET title = 'Changed' WHERE id = %s", (job_id,))
        raw.commit()
        changed = raw.execute(
            "SELECT content_updated_at FROM ats_jobs WHERE id = %s", (job_id,)
        ).fetchone()[0]
        assert changed > expected
    assert flusso(DSN)["aggiornate"] == 1


def test_validate_constraint_solo_con_indici_e_backfill_pronti(db):
    stats = backfill(DSN, apply=True, steps="C", sleep_ms=0)
    assert set(stats["validated"]) == {
        "ats_jobs_posted_at_minima_check",
        "ats_jobs_absence_count_check",
    }
    rows = db.execute(
        "SELECT conname, convalidated FROM pg_constraint "
        "WHERE conname IN ('ats_jobs_posted_at_minima_check', 'ats_jobs_absence_count_check')"
    ).fetchall()
    assert len(rows) == 2
    assert all(row[1] for row in rows)


def test_backlog_zombie_dry_run_e_batch_limitato(db, tmp_path, monkeypatch):
    _company(db, "zombie")
    _seed_jobs(db, "zombie", 150, zombie=True)

    dry = backlog_wave2.esegui(
        DSN, kind="zombie", max_per_tenant=10, max_total=30
    )
    assert dry["dry_run"] is True
    assert dry["closed"] == 0
    assert dry["report"]["selected"] == 10
    assert db.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NOT NULL").fetchone()[0] == 0

    applied = backlog_wave2.esegui(
        DSN, kind="zombie", max_per_tenant=10, max_total=30, apply=True
    )
    assert applied["closed"] == 10
    assert db.execute(
        "SELECT count(*) FROM ats_jobs WHERE closure_reason = 'zombie_365d_backlog'"
    ).fetchone()[0] == 10
    monkeypatch.setattr("nivult.ats.esporta.CARTELLA", str(tmp_path / "exports"))
    assert flusso(DSN)["chiuse"] == 10


def test_backlog_assenti_non_allenta_il_regime(db):
    _company(db, "absent")
    _seed_jobs(db, "absent", 100, absent=True)
    assert expira(DSN) == 0

    dry = backlog_wave2.esegui(
        DSN, kind="last_seen", max_per_tenant=5, max_total=10
    )
    assert dry["report"]["selected"] == 5
    applied = backlog_wave2.esegui(
        DSN, kind="last_seen", max_per_tenant=5, max_total=10, apply=True
    )
    assert applied["closed"] == 5
    assert db.execute(
        "SELECT count(*) FROM ats_jobs WHERE closure_reason = 'last_seen_7d_backlog'"
    ).fetchone()[0] == 5


def test_backlog_jsonld_non_bypassa_la_prova_di_vita(db):
    db.execute(
        "INSERT INTO ats_platforms (id, name, api_type) VALUES ('jsonld', 'JSON-LD', 'html')"
    )
    db.execute(
        "INSERT INTO ats_companies (platform_id, slug, last_ok_at, lettura_parziale) "
        "VALUES ('jsonld', 'sitemap', now(), false)"
    )
    db.execute(
        "INSERT INTO ats_jobs (platform_id, slug, external_id, title, url, posted_at, active_since) "
        "VALUES ('jsonld', 'sitemap', '1', 'Role', 'https://example.com/1', now(), now())"
    )
    db.execute("UPDATE ats_jobs SET fetched_at = now() - interval '8 days'")
    db.execute(
        "UPDATE ats_jobs SET absent_since = now() - interval '8 days', absence_count = 2"
    )

    result = backlog_wave2.esegui(DSN, kind="last_seen", apply=True)

    assert result["report"]["selected"] == 0
    assert result["closed"] == 0
    assert db.execute("SELECT expired_at FROM ats_jobs").fetchone()[0] is None
