import os
from datetime import UTC, datetime

import psycopg
import pytest

from nivult.ats.esporta import flusso
from nivult.ats.mantenimento import expira

DSN = os.environ.get("NIVULT_TEST_ATS_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="richiede un PostgreSQL ATS isolato esplicito")


@pytest.fixture
def db():
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE ats_jobs, ats_companies, ats_platforms CASCADE")
        conn.execute(
            "INSERT INTO ats_platforms (id, name, api_type) VALUES ('oracle', 'Oracle', 'json')"
        )
        yield conn
        conn.execute("TRUNCATE ats_jobs, ats_companies, ats_platforms CASCADE")


def _company(conn, slug):
    conn.execute(
        "INSERT INTO ats_companies (platform_id, slug, last_ok_at, lettura_parziale) "
        "VALUES ('oracle', %s, now(), false)",
        (slug,),
    )


def _jobs(conn, slug, count, *, assenti=False, zombie=False):
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO ats_jobs (
                platform_id, slug, external_id, title, url, posted_at,
                active_since, fetched_at, absent_since, absence_count)
            VALUES (
                'oracle', %s, %s, 'Role', 'https://example.com/job', now(),
                CASE WHEN %s THEN now() - interval '400 days' ELSE now() - interval '30 days' END,
                CASE WHEN %s THEN now() - interval '8 days' ELSE now() END,
                CASE WHEN %s THEN now() - interval '8 days' ELSE NULL END,
                CASE WHEN %s THEN 2 ELSE 0 END)
            """,
            [
                (slug, f"{slug}-{index}", zombie, assenti, assenti, assenti)
                for index in range(count)
            ],
        )
    if assenti:
        conn.execute(
            "UPDATE ats_jobs SET absent_since = now() - interval '8 days', absence_count = 2 "
            "WHERE slug = %s",
            (slug,),
        )


def test_trigger_date_e_preservazione_di_una_data_valida(db):
    row = db.execute(
        """
        INSERT INTO ats_jobs (platform_id, slug, external_id, title, url,
                              posted_at, created_at, fetched_at)
        VALUES ('oracle', 'date', 'bad', 'Role', 'https://example.com/bad',
                '0202-03-19T15:12:12+00:00', '2026-11-20T00:00:00+00:00',
                '2026-11-20T00:00:00+00:00')
        RETURNING posted_at, posted_at_estimated
        """
    ).fetchone()
    assert row == (datetime(2026, 11, 20, tzinfo=UTC), True)

    db.execute(
        "UPDATE ats_jobs SET posted_at = '2019-01-01' WHERE external_id = 'bad'"
    )
    assert db.execute(
        "SELECT posted_at FROM ats_jobs WHERE external_id = 'bad'"
    ).fetchone()[0] == datetime(2026, 11, 20, tzinfo=UTC)


def test_riapertura_reimposta_active_since_e_assenze(db):
    db.execute(
        """
        INSERT INTO ats_jobs (platform_id, slug, external_id, title, url, posted_at,
                              active_since, absent_since, absence_count)
        VALUES ('oracle', 'life', '1', 'Role', 'https://example.com/1', now(),
                now() - interval '400 days', now() - interval '8 days', 2)
        """
    )
    db.execute(
        "UPDATE ats_jobs SET expired_at = now(), closure_reason = 'zombie_365d' "
        "WHERE external_id = '1'"
    )
    db.execute("UPDATE ats_jobs SET expired_at = NULL WHERE external_id = '1'")
    active_since, absent_since, absence_count, reason = db.execute(
        "SELECT active_since, absent_since, absence_count, closure_reason "
        "FROM ats_jobs WHERE external_id = '1'"
    ).fetchone()
    assert active_since > datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert absent_since is None
    assert absence_count == 0
    assert reason is None


def test_circuit_breaker_tenant_e_chiusure_piccole_reali(db):
    _company(db, "big")
    _company(db, "small")
    _jobs(db, "big", 100, assenti=True)
    _jobs(db, "small", 2, assenti=True)

    assert expira(DSN) == 2
    assert db.execute(
        "SELECT count(*) FROM ats_jobs WHERE slug = 'big' AND expired_at IS NULL"
    ).fetchone()[0] == 100
    assert db.execute(
        "SELECT closure_reason FROM ats_jobs WHERE slug = 'small' LIMIT 1"
    ).fetchone()[0] == "last_seen_7d"


def test_backlog_zombie_non_attiva_breaker_e_rispetta_i_limiti(db):
    _company(db, "zombie")
    _jobs(db, "zombie", 150, zombie=True)

    assert expira(DSN) == 100
    assert db.execute(
        "SELECT count(*) FROM ats_jobs WHERE closure_reason = 'zombie_365d'"
    ).fetchone()[0] == 100
    assert expira(DSN) == 0
    assert db.execute(
        "SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL"
    ).fetchone()[0] == 50


def test_batch_zombie_attraversa_il_delta_senza_breaker(db, tmp_path, monkeypatch):
    _company(db, "zombie")
    _jobs(db, "zombie", 150, zombie=True)
    db.execute(
        "UPDATE ats_jobs SET created_at = now() - interval '2 days', "
        "content_updated_at = now() - interval '2 days'"
    )
    assert expira(DSN) == 100
    monkeypatch.setattr("nivult.ats.esporta.CARTELLA", str(tmp_path / "exports"))

    stats = flusso(DSN)

    assert stats["chiuse"] == 100
    assert stats["nuove"] == 0
    assert db.execute(
        "SELECT valore FROM ats_delta_state WHERE chiave = 'flusso'"
    ).fetchone() is not None
