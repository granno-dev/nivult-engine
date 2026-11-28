from __future__ import annotations

import gzip
import json
import os
import threading
import time
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
    full = html[html.index('document.getElementById("activity-log-full")'):
                html.index('document.getElementById("usage-non-itemized")')]
    assert "scaricaExportRecente" in compact
    assert 'class="w-24 shrink-0"' in compact
    assert "Download again · 0 credits" in compact
    assert "const retry = e.download_id ?" in compact
    assert "scaricaExportRecente" in full
    assert "Download again · 0 credits" in full
    assert "const retry = e.download_id ?" in full
    assert "async function scaricaExportRecente(downloadId)" in html
    assert 'api("/portale/export-file/" + encodeURIComponent(downloadId))' in html
    assert 'const legacyTrial = !attivo && volume > 500' in html
    assert '"Legacy one-time trial"' in html
    assert "new accounts receive 500 credits" in html
    assert "new trials: 500 credits" in html


def test_full_activity_log_has_deterministic_columns_and_mobile_stacking():
    html = (ROOT / "vetrina" / "account.html").read_text()
    css = html[html.index(".activity-event {"):html.index(".index-main {")]
    assert "grid-template-columns: 190px 92px 190px minmax(0,1fr) 72px" in css
    assert "minmax(150px,.8fr)" not in css
    assert "minmax(140px,.8fr)" not in css
    assert "@media (max-width: 700px)" in css
    assert "grid-template-columns: minmax(0,1fr) auto" in css
    for child, placement in [
            (1, "grid-column: 1; grid-row: 1"),
            (2, "grid-column: 2; grid-row: 1"),
            (3, "grid-column: 1; grid-row: 2"),
            (4, "grid-column: 1 / -1; grid-row: 3"),
            (5, "grid-column: 2; grid-row: 2")]:
        assert f".activity-event summary > :nth-child({child})" in css
        assert placement in css


def test_unlocked_companies_combobox_selection_and_drawer_contract():
    html = (ROOT / "vetrina" / "account.html").read_text()
    source = html[html.index("let unlockedCompanyQuery"):html.index("function apriUso(")]
    css = html[html.index(".unlocked-picker {"):html.index(".explorer-tabs {")]
    for marker in [
            'role="combobox"', 'aria-autocomplete="list"', 'aria-haspopup="listbox"',
            'aria-multiselectable="true"', 'role="option"', 'type="checkbox"',
            "unlockedCompanySelected", "Clear selection", "Clear company search",
            'event.key === "Escape"', '["ArrowDown", "ArrowUp"]',
            'event.key === "Enter"', "aria-activedescendant",
            "unlockedCompanyLoading", 'btn.setAttribute("aria-busy", "true")']:
        assert marker in source
    assert 'api("/portale/token-azienda"' in source
    assert 'method: "POST", body: JSON.stringify({ company_ref: r.ref })' in source
    assert "await apriAziendaDaJob(token)" in source
    assert 'location.hash = "#explorer"' not in source
    assert 'cambiaExplorer("query")' not in source
    assert "logoRivelato(logoRecord" in source
    assert ".app-input.unlocked-search" in css
    assert html.index('id="explorer"') < html.index('id="dettaglio"')
    assert html.index('id="settings"') < html.index('id="dettaglio"') < html.index('id="export-modale"')
    assert "revealInfo[token]" in source
    assert "if (!response.ok)" in source
    assert "esc(name)" in source
    assert 'onclick="apriAziendaSbloccata(${index}, this)"' in source
    assert "grid-template-columns: repeat(2,minmax(0,1fr))" in css
    assert "max-height: 280px" in css
    assert "overflow-y: auto" in css
    assert "@media (max-width: 639px)" in css
    assert "unlockedCompanySelected.clear();" in html
    assert "unlockedCompanyLoading.clear();" in html
    assert "disegnaAziendeSbloccate();" in html


@pytest.fixture
def rich_export_db(tmp_path):
    path = tmp_path / "rich-export.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute("""
            CREATE TABLE offerte(
                raw VARCHAR, posted_at TIMESTAMP, id VARCHAR, title VARCHAR,
                country VARCHAR, language VARCHAR, seniority VARCHAR, remote VARCHAR,
                category VARCHAR, technologies VARCHAR[], ats VARCHAR)
        """)
        connection.execute("""
            CREATE TABLE aziende(
                raw VARCHAR, company VARCHAR, country VARCHAR, industry VARCHAR,
                employees BIGINT, technologies VARCHAR[], ats VARCHAR, company_slug VARCHAR)
        """)
        jobs = [
            {"id": "a", "title": "Senior Data Engineer", "description": "Kubernetes platform",
             "country": "IT", "language": "en", "seniority": "senior", "remote": "remote",
             "category": "Engineering", "technologies": ["Python", "SAP"], "ats": "greenhouse",
             "posted_at": "2026-11-20", "employment_type": "full_time", "source_type": "direct",
             "salary_min": 50000, "salary_max": 90000},
            {"id": "b", "title": "Data Analyst", "description": "SQL reports",
             "country": "DE", "language": "de", "seniority": "mid", "remote": "hybrid",
             "category": "Data & Analytics", "technologies": ["Python"], "ats": "workday",
             "posted_at": "2026-11-10", "employment_type": "part_time",
             "source_type": "public_board", "salary_min": None, "salary_max": None},
            {"id": "c", "title": "SAP Intern", "description": "Excel support",
             "country": "IT", "language": "it", "seniority": "intern", "remote": "onsite",
             "category": "Engineering", "technologies": ["SAP", "Excel"], "ats": "greenhouse",
             "posted_at": "2026-11-01", "employment_type": "internship", "source_type": "direct",
             "salary_min": 20000, "salary_max": 30000},
        ]
        connection.executemany(
            "INSERT INTO offerte VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(json.dumps(row), row["posted_at"], row["id"], row["title"], row["country"],
              row["language"], row["seniority"], row["remote"], row["category"],
              row["technologies"], row["ats"]) for row in jobs])
        companies = [
            {"company": "Alpha", "country": "IT", "industry": "software", "employees": 100,
             "technologies": ["Python", "SAP"], "ats": "greenhouse", "company_slug": "alpha",
             "active_jobs": 5},
            {"company": "Beta", "country": "DE", "industry": "software", "employees": 500,
             "technologies": ["Python"], "ats": "workday", "company_slug": "beta", "active_jobs": 1},
            {"company": "Gamma", "country": "IT", "industry": "finance", "employees": 20,
             "technologies": ["SAP", "Excel"], "ats": "greenhouse", "company_slug": "gamma",
             "active_jobs": 10},
        ]
        connection.executemany(
            "INSERT INTO aziende VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(json.dumps(row), row["company"], row["country"], row["industry"],
              row["employees"], row["technologies"], row["ats"], row["company_slug"])
             for row in companies])
    previous = dati._percorso
    dati.apri(str(path))
    yield path
    if dati._con is not None:
        dati._con.close()
    dati._con = None
    dati._con_mtime = None
    dati._percorso = previous


def _export_ids(kind: str, filters: dict, path) -> tuple[int, list[str]]:
    normalized = dati.normalizza_filtri_export(filters, kind)
    if kind == "companies":
        count = dati.stima_export_aziende(normalized)
        written = dati.scrivi_export_aziende(normalized, str(path), 100)
    else:
        count = dati.stima_export(normalized)
        written = dati.scrivi_export(normalized, str(path), 100)
    assert count == written
    with gzip.open(path, "rt", encoding="utf-8") as source:
        rows = [json.loads(line) for line in source]
    assert written == len(rows)
    key = "company" if kind == "companies" else "id"
    return count, [row[key] for row in rows]


@pytest.mark.parametrize(("filters", "expected"), [
    ({"countries": "it,de"}, {"a", "b", "c"}),
    ({"technologies": ["python", "sap"]}, {"a", "b", "c"}),
    ({"technologies": ["python", "sap"], "tech_match": "all"}, {"a"}),
    ({"categories": ["Engineering"]}, {"a", "c"}),
    ({"seniority": "senior,mid"}, {"a", "b"}),
    ({"remote": ["remote", "hybrid"]}, {"a", "b"}),
    ({"employment_type": "full_time"}, {"a"}),
    ({"language": "en,it"}, {"a", "c"}),
    ({"has_salary": True}, {"a", "c"}),
    ({"has_salary": False}, {"b"}),
    ({"salary_min": 40000}, {"a"}),
    ({"salary_max": 85000}, {"c"}),
    ({"q": "Kubernetes"}, {"a"}),
    ({"title": "Senior"}, {"a"}),
    ({"posted_after": "2026-11-15"}, {"a"}),
    ({"posted_before": "2026-11-15"}, {"b", "c"}),
    ({"source_type": "direct"}, {"a", "c"}),
    ({"source_type": "public"}, {"b"}),
    ({"ats": "greenhouse"}, {"a", "c"}),
    ({"exclude_ats": "greenhouse"}, {"b"}),
])
def test_duckdb_rich_posting_filters_have_identical_quote_and_download(
        rich_export_db, tmp_path, filters, expected):
    count, ids = _export_ids("postings", filters, tmp_path / "postings.jsonl.gz")
    assert count == len(expected)
    assert set(ids) == expected


@pytest.mark.parametrize(("filters", "expected"), [
    ({"countries": ["IT", "DE"]}, {"Alpha", "Beta", "Gamma"}),
    ({"technologies": ["Python", "SAP"], "tech_match": "all"}, {"Alpha"}),
    ({"industries": "software"}, {"Alpha", "Beta"}),
    ({"employees_min": 50, "employees_max": 500}, {"Alpha", "Beta"}),
    ({"min_active_jobs": 5}, {"Alpha", "Gamma"}),
])
def test_duckdb_rich_company_filters_have_identical_quote_and_download(
        rich_export_db, tmp_path, filters, expected):
    count, names = _export_ids("companies", filters, tmp_path / "companies.jsonl.gz")
    assert count == len(expected)
    assert set(names) == expected


def test_catalog_is_canonical_case_insensitive_ordered_and_injection_safe(rich_export_db):
    assert dati.catalogo_export("technology", "py") == [{"value": "Python", "count": 2}]
    popular = dati.catalogo_export("technology")
    assert [row["value"] for row in popular] == ["Python", "SAP", "Excel"]
    assert popular[0]["count"] == 2
    assert dati.catalogo_export("technology", "DDDDDD") == []
    assert dati.catalogo_export("technology", "%") == []
    assert dati.catalogo_export("country", "i") == [{"value": "IT", "count": 2}]
    assert dati.catalogo_export("industry", "SOFT") == [{"value": "software", "count": 2}]
    assert dati.catalogo_export("technology", "' OR 1=1 --") == []


def test_catalog_http_is_authenticated_free_and_uses_limit(
        rich_export_db, pg_account, portal_client):
    uid = pg_account(0)
    client = portal_client(uid)
    response = client.get("/portale/catalogo/technology?q=py&limit=1")
    assert response.status_code == 200
    assert response.json() == [{"value": "Python", "count": 2}]
    assert client.get("/portale/catalogo/technology?q=DDDDDD").json() == []
    assert client.get("/portale/catalogo/not-a-field").status_code == 404
    assert _billing_state(uid) == (0, 0, 0, 0)


def _reset_catalogo_cache():
    with dati._catalogo_cache_lock:
        dati._catalogo_cache.clear()
        dati._catalogo_fetch_locks.clear()


def test_catalog_sequential_prefixes_and_limits_use_one_duckdb_fetch(
        rich_export_db, monkeypatch):
    _reset_catalogo_cache()
    calls = []
    real_loader = dati._carica_catalogo

    def spy_loader(campo):
        calls.append(campo)
        return real_loader(campo)

    monkeypatch.setattr(dati, "_carica_catalogo", spy_loader)
    cases = [
        ("", 5, ["Python", "SAP", "Excel"]),
        ("s", 25, ["SAP"]),
        ("sa", 5, ["SAP"]),
        ("sap", 25, ["SAP"]),
        ("py", 5, ["Python"]),
        ("DDDDDD", 25, []),
    ]
    for query, limit, expected in cases:
        rows = dati.catalogo_export("technology", query, limit)
        assert [row["value"] for row in rows] == expected
    assert calls == ["technology"]
    key = (str(rich_export_db), "technology")
    assert list(dati._catalogo_cache) == [key]
    cached_mtime, items = dati._catalogo_cache[key]
    assert cached_mtime == os.path.getmtime(rich_export_db)
    assert isinstance(items, tuple)
    assert all(isinstance(item, tuple) and len(item) == 3 for item in items)


def test_catalog_mtime_change_replaces_snapshot_with_one_new_fetch(
        rich_export_db, monkeypatch):
    _reset_catalogo_cache()
    calls = []
    real_loader = dati._carica_catalogo

    def spy_loader(campo):
        calls.append(campo)
        return real_loader(campo)

    monkeypatch.setattr(dati, "_carica_catalogo", spy_loader)
    assert dati.catalogo_export("technology", "sa") == [{"value": "SAP", "count": 2}]
    key = (str(rich_export_db), "technology")
    old_mtime, old_items = dati._catalogo_cache[key]
    stat = os.stat(rich_export_db)
    os.utime(rich_export_db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))

    assert dati.catalogo_export("technology", "py") == [{"value": "Python", "count": 2}]
    assert calls == ["technology", "technology"]
    assert list(dati._catalogo_cache) == [key]
    new_mtime, new_items = dati._catalogo_cache[key]
    assert new_mtime > old_mtime
    assert new_items is not old_items


def test_catalog_concurrent_cold_requests_singleflight(
        rich_export_db, monkeypatch):
    _reset_catalogo_cache()
    calls = []
    real_loader = dati._carica_catalogo

    def spy_loader(campo):
        calls.append(campo)
        time.sleep(0.05)
        return real_loader(campo)

    monkeypatch.setattr(dati, "_carica_catalogo", spy_loader)
    queries = ["s", "sa", "sap", "py", "", "DDDDDD", "s", "py"]
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        results = list(pool.map(
            lambda query: dati.catalogo_export("technology", query, 25), queries))
    assert calls == ["technology"]
    assert [row["value"] for row in results[2]] == ["SAP"]
    assert [row["value"] for row in results[3]] == ["Python"]
    assert [row["value"] for row in results[4]] == ["Python", "SAP", "Excel"]
    assert results[5] == []


@pytest.mark.parametrize("body", [
    {"technologies": ["x" * 121]}, {"tech_match": "sometimes"},
    {"has_salary": "yes"}, {"salary_min": 10, "salary_max": 9},
    {"unexpected": "must-not-be-dropped"}, {"source_type": "unknown"},
])
def test_export_filter_validation_rejects_invalid_or_unsupported_input(body):
    with pytest.raises(ValueError):
        dati.normalizza_filtri_export(body, "postings")


def test_portal_quote_and_download_receive_the_same_normalized_filters(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(100)
    seen = []

    def estimate(filters):
        seen.append(filters)
        return 1

    def write(filters, path, limit):
        seen.append(filters)
        return _writer(1)(filters, path, limit)

    monkeypatch.setattr(dati, "stima_export", estimate)
    monkeypatch.setattr(dati, "scrivi_export", write)
    client = portal_client(uid)
    payload = {"tipo": "postings", "country": "it,de", "technology": "Python,SAP",
               "tech_match": "all", "posted_after": "2026-11-01", "max_rows": 1}
    quote = client.post("/portale/export/stima", json=payload)
    assert quote.status_code == 200
    response = client.post("/portale/export/scarica", json=payload)
    assert response.status_code == 200
    assert seen[0] == seen[1] == seen[2]
    assert seen[0]["countries"] == ["IT", "DE"]
    assert seen[0]["technologies"] == ["Python", "SAP"]
    assert seen[0]["tech_match"] == "all"


def test_unlocked_companies_payload_contains_logo_and_drawer_identity_fields(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(10)
    reference = "greenhouse:acme"
    with psycopg.connect(os.environ["TEST_DATABASE_URL"]) as connection:
        connection.execute(
            "INSERT INTO portale_rivelazioni (user_id, tipo, riferimento) "
            "VALUES (%s, 'azienda', %s)", (uid, reference))
    monkeypatch.setattr(dati, "rivela_azienda", lambda ref: {
        "company": "Acme", "domain": "acme.example", "logo": "https://acme.example/logo.svg"})
    response = portal_client(uid).get("/portale/uso")
    assert response.status_code == 200
    row = response.json()["rivelazioni"][0]
    assert row["ref"] == reference
    assert row["company_domain"] == "acme.example"
    assert row["logo_url"] == "https://acme.example/logo.svg"
    assert row["ats"] == "greenhouse"
    assert row["company_slug"] == "acme"


def test_opensearch_export_query_maps_every_normalized_filter_and_tech_all():
    normalized = dati.normalizza_filtri_export({
        "countries": "it,de", "technologies": ["Python", "SAP"], "tech_match": "all",
        "categories": "Engineering,Data & Analytics", "seniority": "senior,mid",
        "remote": "remote,hybrid", "employment_type": "full_time,contract",
        "language": "en,it", "source_type": "direct,public", "ats": "greenhouse,workday",
        "exclude_ats": "icims", "q": "HR Specialist", "title": "HR",
        "posted_after": "2026-11-01", "posted_before": "2026-11-28",
        "has_salary": True, "salary_min": 40000, "salary_max": 90000,
    }, "postings")
    query = dati._query_export_opensearch(normalized)
    filters = query["bool"]["filter"]
    tech_filters = [item for item in filters if "technologies" in item.get("term", {})
                    or "technologies" in item.get("terms", {})]
    assert tech_filters == [{"term": {"technologies": "python"}},
                            {"term": {"technologies": "sap"}}]
    assert {"terms": {"country": ["it", "de"]}} in filters
    assert {"terms": {"category": ["engineering", "data & analytics"]}} in filters
    assert {"terms": {"seniority": ["senior", "mid"]}} in filters
    assert {"terms": {"remote": ["remote", "hybrid"]}} in filters
    assert {"terms": {"employment_type": ["full_time", "contract"]}} in filters
    assert {"terms": {"language": ["en", "it"]}} in filters
    assert {"terms": {"source_type": ["direct", "public_board"]}} in filters
    assert {"terms": {"ats": ["greenhouse", "workday"]}} in filters
    assert {"term": {"has_salary": True}} in filters
    assert {"range": {"salary_min": {"gte": 40000.0}}} in filters
    assert {"range": {"salary_max": {"lte": 90000.0}}} in filters
    assert query["bool"]["must_not"] == [{"term": {"ats": "icims"}}]
    assert any("multi_match" in item for item in query["bool"]["must"])
    ranges = json.dumps(filters)
    assert "2026-11-01T00:00:00" in ranges
    assert "2026-11-28T00:00:00" in ranges
    with pytest.raises(ValueError, match="unsupported OpenSearch export filter"):
        dati._query_export_opensearch({"unsupported": "raw scan forbidden"})


def test_keyword_quote_does_not_block_concurrent_catalog(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(100)
    seen = []
    monkeypatch.setattr(opensearch, "attivo", lambda: True)

    def slow_opensearch_count(filters):
        seen.append(filters)
        time.sleep(0.3)
        return 42

    def forbidden_duckdb_count(_filters):
        raise AssertionError("OpenSearch must be used instead of the DuckDB raw scan")

    monkeypatch.setattr(dati, "conteggio_export_opensearch", slow_opensearch_count)
    monkeypatch.setattr(dati, "stima_export", forbidden_duckdb_count)
    monkeypatch.setattr(dati, "catalogo_export", lambda campo, q, limite: [
        {"value": "IT", "count": 123}])
    quote_client, catalog_client = portal_client(uid), portal_client(uid)
    payload = {"tipo": "postings", "q": "HR Speci", "max_rows": 10,
               "technologies": ["Python", "SAP"], "tech_match": "all"}
    with ThreadPoolExecutor(max_workers=2) as pool:
        quote_future = pool.submit(quote_client.post, "/portale/export/stima", json=payload)
        time.sleep(0.05)
        started = time.monotonic()
        catalog_future = pool.submit(
            catalog_client.get, "/portale/catalogo/country?q=it&limit=5")
        catalog_response = catalog_future.result(timeout=2)
        catalog_elapsed = time.monotonic() - started
        quote_response = quote_future.result(timeout=2)
    assert catalog_response.status_code == 200
    assert catalog_response.json() == [{"value": "IT", "count": 123}]
    assert catalog_elapsed < 1
    assert quote_response.status_code == 200
    assert quote_response.json()["matching_rows"] == 42
    assert seen[0]["q"] == "HR Speci"
    assert seen[0]["technologies"] == ["Python", "SAP"]
    assert seen[0]["tech_match"] == "all"


def test_keyword_quote_returns_503_without_raw_fallback_when_opensearch_unavailable(
        pg_account, portal_client, monkeypatch):
    uid = pg_account(100)
    monkeypatch.setattr(opensearch, "attivo", lambda: False)

    def forbidden_duckdb_count(_filters):
        raise AssertionError("a keyword must never trigger a raw DuckDB scan")

    monkeypatch.setattr(dati, "stima_export", forbidden_duckdb_count)
    response = portal_client(uid).post(
        "/portale/export/stima", json={"tipo": "postings", "q": "HR Speci"})
    assert response.status_code == 503
    assert "no DuckDB raw scan performed" in response.json()["detail"]


def test_export_quote_abort_controller_and_debounce_contract():
    html = (ROOT / "vetrina" / "account.html").read_text()
    quote = html[html.index("async function stimaExport()"):html.index("function exFitBalance()")]
    assert "exStimaAbort?.abort()" in quote
    assert "new AbortController()" in quote
    assert "signal: controller.signal" in quote
    assert "timedOut = true; controller.abort()" in quote
    assert "setTimeout(stimaExport" in html
    assert 'e.name !== "AbortError"' in html
    assert "setInterval" not in quote
