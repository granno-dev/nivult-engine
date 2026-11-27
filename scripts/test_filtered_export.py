from __future__ import annotations

import gzip
import json
import os
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

import duckdb
import psycopg
import pytest
from fastapi.testclient import TestClient

from nivult.api import app as app_module
from nivult.api_clienti import chiavi, dati, opensearch

ROOT = Path(__file__).resolve().parents[1]


def _require_local_test_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL export tests")
    if dsn.startswith(("postgresql://", "postgres://")):
        host = urlsplit(dsn).hostname
    else:
        host = psycopg.conninfo.conninfo_to_dict(dsn).get("host", "")
    assert host in (None, "", "localhost", "127.0.0.1", "::1") or host.startswith("/")
    return dsn


@pytest.fixture
def pg_account(monkeypatch):
    dsn = _require_local_test_dsn()
    previous_pool = chiavi._pool
    chiavi.configura_pool(None)
    monkeypatch.setattr(chiavi, "_URL", dsn)
    monkeypatch.setenv("DATABASE_URL", dsn)
    created = []

    def create(balance: int) -> str:
        uid = str(uuid.uuid4())
        key_id = uuid.uuid4()
        created.append(uid)
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, email, plan, subscription_status, frequency, crediti_extra) "
                "VALUES (%s, %s, 'basic', 'trialing', 'daily', 0)",
                (uid, f"filtered-export-{uid}@example.test"),
            )
            connection.execute(
                "INSERT INTO api_chiavi "
                "(id, key_hash, label, crediti_mensili, usati_mese, user_id, key_tail) "
                "VALUES (%s, %s, 'Filtered export test', %s, 0, %s, 'exp001')",
                (key_id, key_id.hex * 2, balance, uid),
            )
        return uid

    yield create
    try:
        with psycopg.connect(dsn) as connection:
            for uid in created:
                connection.execute("DELETE FROM api_chiavi WHERE user_id = %s", (uid,))
                connection.execute("DELETE FROM users WHERE id = %s", (uid,))
    finally:
        chiavi.configura_pool(previous_pool)


def _override_uid(application, uid: str) -> None:
    def visit(dependant) -> None:
        for dependency in dependant.dependencies:
            if dependency.name == "uid":
                application.dependency_overrides[dependency.call] = lambda: uid
            visit(dependency)

    for route in application.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            visit(dependant)


@pytest.fixture
def portal_client(monkeypatch, tmp_path):
    import tempfile

    monkeypatch.setenv("B2B_ENABLED", "1")
    monkeypatch.setenv("CORS_ORIGINS", "https://portal.test")
    monkeypatch.setenv("PORTAL_EXPORT_DIR", str(tmp_path / "archive"))
    monkeypatch.setattr(opensearch, "attivo", lambda: False)
    original_mkstemp = tempfile.mkstemp

    def isolated_mkstemp(*args, **kwargs):
        kwargs["dir"] = tmp_path
        return original_mkstemp(*args, **kwargs)

    monkeypatch.setattr(tempfile, "mkstemp", isolated_mkstemp)
    clients = []

    def build(uid: str) -> TestClient:
        application = app_module.create_app()
        _override_uid(application, uid)
        client = TestClient(application, raise_server_exceptions=False)
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.close()


def _billing_state(uid: str) -> tuple[int, int, int, int]:
    dsn = os.environ["TEST_DATABASE_URL"]
    with psycopg.connect(dsn) as connection:
        used, extra = connection.execute(
            "SELECT k.usati_mese, u.crediti_extra "
            "FROM users u JOIN api_chiavi k ON k.user_id = u.id WHERE u.id = %s",
            (uid,),
        ).fetchone()
        count, credits = connection.execute(
            "SELECT count(*), coalesce(sum(crediti), 0) FROM portale_uso "
            "WHERE user_id = %s AND via = 'export' AND azione = 'filtered export'",
            (uid,),
        ).fetchone()
    return used, extra, count, credits


def _writer(rows: int, calls: list[int] | None = None, barrier=None):
    def write(_filters, path: str, limit: int) -> int:
        if calls is not None:
            calls.append(limit)
        if barrier is not None:
            barrier.wait(timeout=20)
        written = min(rows, limit) if limit else rows
        with gzip.open(path, "wt", encoding="utf-8") as output:
            for index in range(written):
                output.write(json.dumps({"id": str(index), "title": f"Job {index}"}) + "\n")
        return written

    return write


def _stub_export(monkeypatch, matching: int, written: int | None = None,
                 writer=None) -> list[int]:
    calls = []
    monkeypatch.setattr(dati, "stima_export", lambda _: matching)
    monkeypatch.setattr(dati, "stima_export_aziende", lambda _: matching)
    selected = writer or _writer(matching if written is None else written, calls)
    monkeypatch.setattr(dati, "scrivi_export", selected)
    monkeypatch.setattr(dati, "scrivi_export_aziende", selected)
    return calls


def _jsonl(response) -> list[dict]:
    return [json.loads(line) for line in gzip.decompress(response.content).splitlines()]


@pytest.mark.parametrize(("kind", "rows", "balance", "cost"), [
    ("postings", 10, 500, 10),
    ("postings", 4576, 5000, 4576),
    ("companies", 10, 500, 30),
])
def test_filtered_export_charges_api_unit_rate_and_emits_exact_rows(
        pg_account, portal_client, monkeypatch, tmp_path, kind, rows, balance, cost):
    uid = pg_account(balance)
    _stub_export(monkeypatch, rows)
    client = portal_client(uid)

    quote = client.post("/portale/export/stima", json={"tipo": kind, "max_rows": rows})
    assert quote.status_code == 200
    estimate = quote.json()
    assert estimate["matching_rows"] == rows
    assert estimate["export_rows"] == rows
    assert estimate["unit_cost"] == (3 if kind == "companies" else 1)
    assert estimate["exact_cost"] == cost
    assert estimate["affordable_rows"] == balance // estimate["unit_cost"]
    assert estimate["max_allowed_rows"] == 100_000
    assert estimate["balance_after_export"] == balance - cost

    response = client.post("/portale/export/scarica", json={"tipo": kind, "max_rows": rows})
    assert response.status_code == 200
    assert response.headers["X-Credits-Charged"] == str(cost)
    assert response.headers["X-Export-Rows"] == str(rows)
    assert len(_jsonl(response)) == rows
    assert _billing_state(uid) == (cost, 0, 1, cost)
    assert response.headers["X-Download-Id"]
    assert list(tmp_path.glob("export-portale-*")) == []
    assert len(list((tmp_path / "archive").glob("*.jsonl.gz"))) == 1


def test_filtered_export_limit_alias_caps_quote_file_and_charge(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(100)
    calls = _stub_export(monkeypatch, 50)
    client = portal_client(uid)
    payload = {"tipo": "postings", "max_rows": 10, "limit": 10}

    quote = client.post("/portale/export/stima", json=payload)
    assert quote.status_code == 200
    assert quote.json()["matching_rows"] == 50
    assert quote.json()["export_rows"] == 10
    assert quote.json()["exact_cost"] == 10

    response = client.post("/portale/export/scarica", json=payload)
    assert response.status_code == 200
    assert calls == [10]
    assert len(_jsonl(response)) == 10
    assert _billing_state(uid) == (10, 0, 1, 10)


def test_saved_export_redownload_is_free_identical_and_owner_only(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(20)
    other_uid = pg_account(20)
    _stub_export(monkeypatch, 1)
    client = portal_client(uid)
    response = client.post("/portale/export/scarica",
                           json={"tipo": "postings", "max_rows": 10})
    assert response.status_code == 200
    download_id = response.headers["X-Download-Id"]
    before = _billing_state(uid)

    again = client.get(f"/portale/export-file/{download_id}",
                       headers={"Origin": "https://portal.test"})
    assert again.status_code == 200
    assert again.content == response.content
    assert again.headers["X-Credits-Charged"] == "0"
    assert "Content-Disposition" in again.headers["Access-Control-Expose-Headers"]
    assert _billing_state(uid) == before

    other = portal_client(other_uid)
    assert other.get(f"/portale/export-file/{download_id}").status_code == 404
    assert client.get("/portale/export-file/not-a-uuid").status_code == 400

    activity = client.get("/portale/uso")
    assert activity.status_code == 200
    event = next(e for e in activity.json()["eventi"] if e["download_id"] == download_id)
    assert event["titolo"] == "1 job record"
    assert _billing_state(uid) == before


@pytest.mark.parametrize(("kind", "balance"), [("postings", 0), ("companies", 0)])
def test_filtered_export_empty_result_is_free(
        pg_account, portal_client, monkeypatch, kind, balance):
    uid = pg_account(balance)
    calls = []

    def unexpected_writer(*_args):
        calls.append(1)
        raise AssertionError("empty exports must not create a file")

    _stub_export(monkeypatch, 0, writer=unexpected_writer)
    client = portal_client(uid)
    quote = client.post("/portale/export/stima", json={"tipo": kind})
    assert quote.status_code == 200
    assert quote.json()["export_rows"] == 0
    assert quote.json()["exact_cost"] == 0
    response = client.post("/portale/export/scarica", json={"tipo": kind})
    assert response.status_code == 404
    assert calls == []
    assert _billing_state(uid) == (0, 0, 0, 0)


@pytest.mark.parametrize(("kind", "balance"), [("postings", 9), ("companies", 2)])
def test_filtered_export_insufficient_balance_is_402_before_file_generation(
        pg_account, portal_client, monkeypatch, kind, balance):
    uid = pg_account(balance)
    calls = []

    def unexpected_writer(*_args):
        calls.append(1)
        raise AssertionError("unaffordable exports must not create a file")

    _stub_export(monkeypatch, 10, writer=unexpected_writer)
    client = portal_client(uid)
    response = client.post("/portale/export/scarica", json={"tipo": kind, "max_rows": 10})
    assert response.status_code == 402
    assert response.json()["detail"]["error"] == "insufficient_credits"
    assert calls == []
    assert _billing_state(uid) == (0, 0, 0, 0)


def test_filtered_export_charges_only_rows_actually_written(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(100)
    _stub_export(monkeypatch, 100, written=7)
    client = portal_client(uid)
    response = client.post("/portale/export/scarica",
                           json={"tipo": "postings", "max_rows": 100})
    assert response.status_code == 200
    assert response.headers["X-Credits-Charged"] == "7"
    assert len(_jsonl(response)) == 7
    assert _billing_state(uid) == (7, 0, 1, 7)


def test_filtered_export_failed_atomic_charge_reports_actual_row_cost(
        pg_account, portal_client, monkeypatch, tmp_path):
    uid = pg_account(100)
    _stub_export(monkeypatch, 100, written=7)
    monkeypatch.setattr(chiavi, "spendi_export_archiviato_per_utente", lambda *args, **kwargs: False)
    client = portal_client(uid)
    response = client.post("/portale/export/scarica",
                           json={"tipo": "postings", "max_rows": 100})
    assert response.status_code == 402
    assert response.json()["detail"]["required_credits"] == 7
    assert response.json()["detail"]["balance"] == 100
    assert _billing_state(uid) == (0, 0, 0, 0)
    assert list(tmp_path.glob("export-portale-*")) == []
    assert not list((tmp_path / "archive").glob("*.jsonl.gz"))


def test_filtered_export_all_is_capped_at_max_allowed_rows(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(200_000)
    _stub_export(monkeypatch, 100_001)
    client = portal_client(uid)
    response = client.post("/portale/export/stima", json={"tipo": "postings"})
    assert response.status_code == 200
    assert response.json()["matching_rows"] == 100_001
    assert response.json()["export_rows"] == 100_000
    assert response.json()["exact_cost"] == 100_000
    assert response.json()["max_allowed_rows"] == 100_000


def test_filtered_export_generation_failure_rolls_back_all_charges(
        pg_account, portal_client, monkeypatch, tmp_path):
    uid = pg_account(100)

    def failing_writer(*_args):
        raise RuntimeError("synthetic generation failure")

    _stub_export(monkeypatch, 10, writer=failing_writer)
    client = portal_client(uid)
    response = client.post("/portale/export/scarica", json={"tipo": "postings"})
    assert response.status_code == 500
    assert _billing_state(uid) == (0, 0, 0, 0)
    assert list(tmp_path.glob("export-portale-*")) == []
    assert not list((tmp_path / "archive").glob("*.jsonl.gz"))


def test_filtered_export_rejects_writer_that_exceeds_approved_limit(
        pg_account, portal_client, monkeypatch, tmp_path):
    uid = pg_account(100)

    def excessive_writer(_filters, path, limit):
        return _writer(limit + 1)(_filters, path, 0)

    _stub_export(monkeypatch, 10, writer=excessive_writer)
    client = portal_client(uid)
    response = client.post("/portale/export/scarica",
                           json={"tipo": "postings", "max_rows": 10})
    assert response.status_code == 500
    assert _billing_state(uid) == (0, 0, 0, 0)
    assert list(tmp_path.glob("export-portale-*")) == []
    assert not list((tmp_path / "archive").glob("*.jsonl.gz"))


def test_filtered_export_unexpected_billing_error_cleans_temporary_file(
        pg_account, portal_client, monkeypatch, tmp_path):
    uid = pg_account(100)
    _stub_export(monkeypatch, 10)

    def failing_charge(*_args, **_kwargs):
        raise RuntimeError("synthetic billing failure")

    monkeypatch.setattr(chiavi, "spendi_export_archiviato_per_utente", failing_charge)
    client = portal_client(uid)
    response = client.post("/portale/export/scarica", json={"tipo": "postings"})
    assert response.status_code == 500
    assert _billing_state(uid) == (0, 0, 0, 0)
    assert list(tmp_path.glob("export-portale-*")) == []
    assert not list((tmp_path / "archive").glob("*.jsonl.gz"))


@pytest.mark.parametrize("value", [0, -1, 100_001, True, 1.5, "not-a-number"])
def test_filtered_export_rejects_invalid_max_rows(
        pg_account, portal_client, monkeypatch, value):
    uid = pg_account(100)
    _stub_export(monkeypatch, 10)
    client = portal_client(uid)
    for path in ("/portale/export/stima", "/portale/export/scarica"):
        response = client.post(path, json={"tipo": "postings", "max_rows": value})
        assert response.status_code == 400
    assert _billing_state(uid) == (0, 0, 0, 0)


def test_postgres_concurrent_downloads_only_one_can_spend_shared_balance(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(10)
    barrier = threading.Barrier(2)
    _stub_export(monkeypatch, 10, writer=_writer(10, barrier=barrier))
    clients = [portal_client(uid), portal_client(uid)]

    def download(client):
        return client.post("/portale/export/scarica",
                           json={"tipo": "postings", "max_rows": 10})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(download, clients))

    assert Counter(response.status_code for response in responses) == Counter({200: 1, 402: 1})
    successful = next(response for response in responses if response.status_code == 200)
    assert len(_jsonl(successful)) == 10
    assert successful.headers["X-Credits-Charged"] == "10"
    assert _billing_state(uid) == (10, 0, 1, 10)
    assert chiavi.saldo_per_utente(uid) == 0


def test_duckdb_export_applies_limit_and_deterministic_freshness_order(
        tmp_path, monkeypatch):
    database = tmp_path / "filtered-export.duckdb"
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE TABLE offerte(raw VARCHAR, posted_at TIMESTAMP, id VARCHAR)")
        connection.execute(
            "CREATE TABLE aziende(raw VARCHAR, company VARCHAR, ats VARCHAR, company_slug VARCHAR)")
        connection.executemany(
            "INSERT INTO offerte VALUES (?, ?, ?)", [
                (json.dumps({"id": "old"}), "2026-01-01", "old"),
                (json.dumps({"id": "a"}), "2026-03-01", "a"),
                (json.dumps({"id": "b"}), "2026-03-01", "b"),
            ])
        connection.executemany(
            "INSERT INTO aziende VALUES (?, ?, ?, ?)", [
                (json.dumps({"company": "Quiet", "jobs_posted_30d": 0, "active_jobs": 20}),
                 "Quiet", "greenhouse", "quiet"),
                (json.dumps({"company": "Fresh", "jobs_posted_30d": 8, "active_jobs": 2}),
                 "Fresh", "greenhouse", "fresh"),
                (json.dumps({"company": "Active", "jobs_posted_30d": 2, "active_jobs": 30}),
                 "Active", "greenhouse", "active"),
            ])
    monkeypatch.setattr(dati, "_percorso", str(database))

    postings_path = tmp_path / "postings.jsonl.gz"
    companies_path = tmp_path / "companies.jsonl.gz"
    assert dati.scrivi_export({}, str(postings_path), 2) == 2
    assert dati.scrivi_export_aziende({}, str(companies_path), 2) == 2

    with gzip.open(postings_path, "rt", encoding="utf-8") as exported:
        postings = [json.loads(line) for line in exported]
    with gzip.open(companies_path, "rt", encoding="utf-8") as exported:
        companies = [json.loads(line) for line in exported]
    assert [row["id"] for row in postings] == ["b", "a"]
    assert [row["company"] for row in companies] == ["Fresh", "Active"]


def test_export_modal_copy_controls_and_server_quote_contract():
    html = (ROOT / "vetrina" / "account.html").read_text()
    start = html.index("let exStimaTimer")
    end = html.index('document.querySelectorAll("[data-route-link]', start)
    export_script = html[start:end]
    for marker in [
        'id="ex-max-rows"', 'id="ex-max-100"', 'id="ex-max-500"',
        'id="ex-max-1000"', 'id="ex-max-all"', "Matching dataset · not included",
        "Included in this download", "Exact cost", "Current balance", "Balance after export",
        "most recent", "Fit my balance", "Top up credits", "Max 100k",
        "Same rate as API · 1 credit/job · 3 credits/company", 'id="ex-cap-nota"',
        'id="ex-avviso-metriche"', 'id="ex-metric-saldo"', 'id="ex-metric-costo"',
        'id="ex-metric-mancano"', "Safety cap: only ", "rows are excluded and are not charged.",
    ]:
        assert marker in html
    for old_copy in [
        "1 credit / 10 job records", "1 credit / 10 jobs", "1 credit = 1 company record",
        "Portal filtered export pricing", "Top up with at least ", "You're short ",
        "All is limited to ",
    ]:
        assert old_copy not in html
    assert "f.max_rows = Number(maxRows)" in export_script
    assert "d.matching_rows" in export_script
    assert "d.export_rows" in export_script
    assert "d.exact_cost" in export_script
    assert "d.balance_after_export" in export_script
    assert 'getElementById("ex-saldo-attuale").textContent = exFmt(d.saldo)' in export_script
    assert "exMostraSaldoInsufficiente(d)" in export_script
    assert 'getElementById("ex-metric-mancano").textContent = exFmt(mancano)' in export_script
    assert "exImpostaMax(String(rows))" in export_script
    assert "d.matching_rows > d.max_allowed_rows" in export_script
    assert 'if (r.status === 402) {\n    await stimaExport();' in export_script
    assert "avviso.innerHTML" not in export_script


def test_account_startup_keeps_topup_pricing_helper():
    html = (ROOT / "vetrina" / "account.html").read_text()
    assert "function euroPer(crediti)" in html
    assert "const e = euroPer(c)" in html
    assert html.index("function euroPer(crediti)") < html.index("aggiornaBuy();")


def test_saved_report_activity_alignment_and_legacy_trial_copy():
    html = (ROOT / "vetrina" / "account.html").read_text()
    compact = html[html.index("function eventoCompatto("):html.index("async function caricaUso(")]
    assert "scaricaExportRecente" in compact
    assert 'class="w-24 shrink-0"' in compact
    assert "Download again · 0 credits" in compact
    assert "async function scaricaExportRecente(downloadId)" in html
    assert 'api("/portale/export-file/" + encodeURIComponent(downloadId))' in html
    assert 'const legacyTrial = !attivo && volume > 500' in html
    assert '"Legacy one-time trial"' in html
    assert "new accounts receive 500 credits" in html
    assert "new trials: 500 credits" in html
