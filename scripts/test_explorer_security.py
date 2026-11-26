from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

import duckdb
import psycopg
import pytest
from fastapi.testclient import TestClient

from nivult.api import app as app_module
from nivult.api_clienti import chiavi, dati, explorer, vivo

UID_A = "6c2c38de-7bb4-4f1e-b72b-92df9a0d06f1"
UID_B = "f6ab45a2-e1c9-4fc3-9914-cde0f12de38e"
COMPANY = "Zephyr Quantum Labs"
BRAND = "zephyrquantum"
DOMAIN = f"https://www.{BRAND}.example"
SLUG = "zephyr-quantum-7f3e-secret"
REF = f"greenhouse:{SLUG}"
COMPANY_ID = "company-7d48c61e-secret"
LOGO = f"https://cdn.{BRAND}.example/private-logo.svg"
APPLY = f"https://apply.{BRAND}.example/private/requisition-914"
SOURCE = "source-7f3e-private"
DESCRIPTION_MARKER = "requisition-blue-otter-914"
DESCRIPTION = f"{COMPANY} confidential description {DESCRIPTION_MARKER}"
DISJOINT_COMPANY = "Acme Corporation"
DISJOINT_BRAND = "zqvxunique"
DISJOINT_DOMAIN = f"{DISJOINT_BRAND}.example"
DISJOINT_SLUG = "acme-corporation-disjoint"
RAW_INNER_CURSOR = json.dumps(["2026-01-01T00:00:00", REF])

SECRET_VALUES = {
    COMPANY,
    BRAND,
    DOMAIN,
    f"www.{BRAND}.example",
    SLUG,
    REF,
    COMPANY_ID,
    LOGO,
    APPLY,
    SOURCE,
    DESCRIPTION,
    DESCRIPTION_MARKER,
}
FORBIDDEN_KEYS = {
    "company",
    "company_id",
    "company_slug",
    "company_domain",
    "domain",
    "logo",
    "logo_url",
    "website",
    "url",
    "apply",
    "apply_url",
    "application_url",
    "source",
    "source_type",
    "description",
    "summary",
    "summary_source",
    "external_id",
    "job_sources",
    "ref",
}


def _secret_job() -> dict:
    return {
        "id": "public-posting-1",
        "title": f"{COMPANY} | {BRAND} Platform Engineer",
        "ats": "greenhouse",
        "country": "IT",
        "city": "Milano",
        "seniority": "senior",
        "remote": "remote",
        "category": "engineering",
        "posted_at": "2026-01-01T00:00:00",
        "technologies": ["Python"],
        "company": COMPANY,
        "company_id": COMPANY_ID,
        "company_slug": SLUG,
        "company_domain": DOMAIN,
        "logo_url": LOGO,
        "url": APPLY,
        "apply_url": APPLY,
        "source": SOURCE,
        "source_type": SOURCE,
        "description": DESCRIPTION,
        "summary": DESCRIPTION,
        "summary_source": SOURCE,
        "external_id": COMPANY_ID,
        "job_sources": [{"url": APPLY, "source": SOURCE}],
    }


def _secret_company() -> dict:
    return {
        "country": "IT",
        "industry": "software",
        "employees": 123,
        "employee_range": "100-249",
        "n_tecnologie": 4,
        "open_jobs_count": 7,
        "active_jobs": 7,
        "company": COMPANY,
        "company_id": COMPANY_ID,
        "company_slug": SLUG,
        "ats": "greenhouse",
        "domain": DOMAIN,
        "logo": LOGO,
        "website": DOMAIN,
        "source": SOURCE,
        "description": DESCRIPTION,
        "ref": REF,
    }


def _secret_profile() -> dict:
    return {"company": COMPANY, "domain": DOMAIN, "website": DOMAIN, "logo": LOGO}


def _walk_json(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk_json(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_json(item)


def _assert_zero_leak(payload) -> None:
    for key, _ in _walk_json(payload):
        assert str(key).casefold() not in FORBIDDEN_KEYS
    serialized = json.dumps(payload, ensure_ascii=False).casefold()
    for secret in SECRET_VALUES:
        assert secret.casefold() not in serialized


def _assert_revealed_job(payload: dict) -> None:
    assert payload["revealed"] is True
    assert payload["company"] == COMPANY
    assert payload["company_slug"] == SLUG
    assert payload["company_domain"] == DOMAIN
    assert payload["logo_url"] == LOGO
    assert payload["url"] == APPLY
    assert payload["source_type"] == SOURCE
    assert payload["description"] == DESCRIPTION


def _assert_revealed_company(payload: dict) -> None:
    assert payload["revealed"] is True
    assert payload["company"] == COMPANY
    assert payload["company_slug"] == SLUG
    assert payload["domain"] == DOMAIN
    assert payload["logo"] == LOGO
    assert payload["website"] == DOMAIN
    assert payload["active_jobs"] == 7


@pytest.fixture(autouse=True)
def explorer_environment(monkeypatch):
    monkeypatch.setenv("EXPLORER_REVEAL_SECRET", "explorer-test-secret-32-bytes-minimum")
    monkeypatch.setenv("B2B_ENABLED", "1")
    monkeypatch.setenv("CORS_ORIGINS", "https://portal.test")


def _override_dependencies(application, uid: str | None) -> None:
    def visit(dependant) -> None:
        for dependency in dependant.dependencies:
            if dependency.name == "conn":
                application.dependency_overrides[dependency.call] = lambda: None
            if uid is not None and dependency.name == "uid":
                application.dependency_overrides[dependency.call] = lambda: uid
            visit(dependency)

    for route in application.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is not None:
            visit(dependant)


@pytest.fixture
def portal_client(monkeypatch):
    monkeypatch.setattr(chiavi, "consuma_quota_explorer", lambda *args: None)
    clients = []

    def build(uid: str | None = UID_A) -> TestClient:
        application = app_module.create_app()
        _override_dependencies(application, uid)
        client = TestClient(application)
        clients.append(client)
        return client

    yield build
    for client in clients:
        client.close()


def _stub_projection(monkeypatch, revealed: set[str]) -> None:
    monkeypatch.setattr(
        dati,
        "cerca_portale",
        lambda *args: ([_secret_job()], RAW_INNER_CURSOR, 1),
    )
    monkeypatch.setattr(
        dati,
        "cerca_aziende_portale",
        lambda *args: ([_secret_company()], None, 1),
    )
    monkeypatch.setattr(vivo, "rivela_offerta", lambda offer_id: _secret_job())
    monkeypatch.setattr(
        chiavi,
        "rivelate_per_utente",
        lambda uid, kind, references: set(references) & revealed,
    )
    monkeypatch.setattr(
        chiavi,
        "e_rivelata",
        lambda uid, kind, reference: reference in revealed,
    )
    monkeypatch.setattr(
        dati,
        "profili_aziende",
        lambda references: {REF: _secret_profile()} if references else {},
    )


@pytest.mark.parametrize("kind", ["job", "detail", "company"])
def test_dto_recursive_zero_leak_and_revealed_projection(kind):
    row = _secret_company() if kind == "company" else _secret_job()
    dto = explorer.company_dto if kind == "company" else (
        explorer.detail_dto if kind == "detail" else explorer.job_dto
    )

    preview = dto(row, False, "opaque-token", "opaque-group")
    _assert_zero_leak(preview)
    assert preview["revealed"] is False
    if kind != "company":
        assert "Hidden company" in preview["title"]

    revealed = dto(row, True, "opaque-token", "opaque-group")
    if kind == "company":
        _assert_revealed_company(revealed)
    else:
        _assert_revealed_job(revealed)


def test_http_jobs_companies_and_detail_projection_before_and_after_reveal(
        monkeypatch, portal_client):
    revealed = set()
    _stub_projection(monkeypatch, revealed)
    client = portal_client()

    jobs = client.post("/portale/cerca", json={})
    companies = client.post("/portale/cerca-aziende", json={})
    detail = client.post("/portale/offerta-dettaglio", json={"id": "public-posting-1"})

    for response in (jobs, companies, detail):
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        _assert_zero_leak(response.json())
    assert "Hidden company" in jobs.json()["data"][0]["title"]
    assert jobs.json()["next_cursor"]
    assert RAW_INNER_CURSOR.casefold() not in json.dumps(jobs.json()).casefold()

    revealed.add(REF)
    revealed_jobs = client.post("/portale/cerca", json={})
    revealed_companies = client.post("/portale/cerca-aziende", json={})
    revealed_detail = client.post(
        "/portale/offerta-dettaglio", json={"id": "public-posting-1"})

    _assert_revealed_job(revealed_jobs.json()["data"][0])
    _assert_revealed_company(revealed_companies.json()["data"][0])
    _assert_revealed_job(revealed_detail.json()["offerta"])


def _forbid_reveal_backend(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("backend must not be reached")

    monkeypatch.setattr(dati, "rivela_azienda", forbidden)
    monkeypatch.setattr(chiavi, "rivela_per_utente", forbidden)
    monkeypatch.setattr(chiavi, "registra_uso", forbidden)
    monkeypatch.setattr(chiavi, "saldo_per_utente", forbidden)


def _one_bit_tamper(token: str) -> str:
    raw = bytearray(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    raw[-1] ^= 1
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_reveal_token_one_bit_tamper_returns_400(monkeypatch, portal_client):
    _forbid_reveal_backend(monkeypatch)
    token = _one_bit_tamper(explorer.reveal_token(UID_A, REF))
    response = portal_client().post("/portale/rivela-azienda", json={"reveal_token": token})
    assert response.status_code == 400


def test_user_a_reveal_token_as_user_b_returns_403(monkeypatch, portal_client):
    _forbid_reveal_backend(monkeypatch)
    token = explorer.reveal_token(UID_A, REF)
    response = portal_client(UID_B).post(
        "/portale/rivela-azienda", json={"reveal_token": token})
    assert response.status_code == 403


def test_expired_reveal_token_returns_400(monkeypatch, portal_client):
    _forbid_reveal_backend(monkeypatch)
    token = explorer._seal({
        "kind": "reveal",
        "company_id": REF,
        "user_id": UID_A,
        "exp": int(time.time()) - 1,
    })
    response = portal_client().post("/portale/rivela-azienda", json={"reveal_token": token})
    assert response.status_code == 400


def test_cursor_does_not_expose_company_ref_and_sealed_deep_page_is_rejected(portal_client):
    cursor = explorer.seal_cursor(UID_A, "jobs", RAW_INNER_CURSOR, 2)
    assert REF not in cursor
    assert SLUG not in cursor
    assert explorer.open_cursor(cursor, UID_A, "jobs") == (RAW_INNER_CURSOR, 2)

    deep = explorer.seal_cursor(UID_A, "jobs", RAW_INNER_CURSOR, explorer.MAX_PAGE + 1)
    response = portal_client().post("/portale/cerca", json={"cursor": deep})
    assert response.status_code == 400


@pytest.mark.parametrize("body", [
    {"limit": 500},
    {"page": 50},
    {"page": explorer.MAX_PAGE + 1},
    {"offset": 81},
])
def test_http_preview_page_size_and_depth_are_blocked(monkeypatch, portal_client, body):
    def forbidden(*args, **kwargs):
        raise AssertionError("data layer must not be reached")

    monkeypatch.setattr(dati, "cerca_portale", forbidden)
    monkeypatch.setattr(vivo, "cerca_portale", forbidden)
    response = portal_client().post("/portale/cerca", json=body)
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "preview_limit"


def test_twenty_first_explorer_request_gets_429_and_retry_after(monkeypatch, portal_client):
    calls = {"quota": 0, "query": 0}

    def quota(uid, ip):
        calls["quota"] += 1
        if calls["quota"] == 21:
            raise chiavi.QuotaExplorer(42, "user_minute")

    def query(*args):
        calls["query"] += 1
        return [], None, 0

    monkeypatch.setattr(chiavi, "consuma_quota_explorer", quota)
    monkeypatch.setattr(dati, "cerca_portale", query)
    monkeypatch.setattr(chiavi, "rivelate_per_utente", lambda *args: set())
    monkeypatch.setattr(dati, "profili_aziende", lambda references: {})
    client = portal_client()

    responses = [client.post("/portale/cerca", json={}) for _ in range(21)]
    assert [response.status_code for response in responses] == [200] * 20 + [429]
    assert responses[-1].headers["Retry-After"] == "42"
    assert responses[-1].headers["Cache-Control"] == "no-store"
    assert calls == {"quota": 21, "query": 20}


def test_explorer_requires_session_without_touching_database(portal_client):
    response = portal_client(None).post("/portale/cerca", json={})
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "no-store"


def test_direct_company_domain_and_text_filters_are_rejected(monkeypatch, portal_client):
    def forbidden(*args, **kwargs):
        raise AssertionError("data layer must not be reached")

    monkeypatch.setattr(dati, "cerca_portale", forbidden)
    monkeypatch.setattr(vivo, "cerca_portale", forbidden)
    monkeypatch.setattr(dati, "cerca_aziende_portale", forbidden)
    client = portal_client()

    for key in ("company", "domain", "company_domain", "company_slug"):
        response = client.post("/portale/cerca", json={key: COMPANY})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "company_filter_unavailable"
    for key in ("q", "company", "domain", "company_domain", "company_slug"):
        response = client.post("/portale/cerca-aziende", json={key: COMPANY})
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "company_filter_unavailable"


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://attacker.example"},
])
def test_cross_site_company_reveal_is_rejected_before_data_or_charging(
        monkeypatch, portal_client, headers):
    _forbid_reveal_backend(monkeypatch)
    token = explorer.reveal_token(UID_A, REF)
    response = portal_client().post(
        "/portale/rivela-azienda", json={"reveal_token": token}, headers=headers)
    assert response.status_code == 403
    assert response.headers["Cache-Control"] == "no-store"


def test_insufficient_balance_returns_no_company_data_and_no_charge(
        monkeypatch, portal_client):
    calls = []
    monkeypatch.setattr(dati, "rivela_azienda", lambda reference: _secret_company())

    def insufficient(uid, kind, reference):
        calls.append((uid, kind, reference))
        return "senza_crediti"

    def forbidden(*args, **kwargs):
        raise AssertionError("a failed reveal cannot write usage or return a balance")

    monkeypatch.setattr(chiavi, "rivela_per_utente", insufficient)
    monkeypatch.setattr(chiavi, "registra_uso", forbidden)
    monkeypatch.setattr(chiavi, "saldo_per_utente", forbidden)
    token = explorer.reveal_token(UID_A, REF)
    response = portal_client().post("/portale/rivela-azienda", json={"reveal_token": token})

    assert response.status_code == 429
    assert response.json() == {"detail": "3 credits required — top up from the dashboard"}
    _assert_zero_leak(response.json())
    assert calls == [(UID_A, "azienda", REF)]


def test_successful_company_reveal_returns_allowed_data(monkeypatch, portal_client):
    monkeypatch.setattr(dati, "rivela_azienda", lambda reference: _secret_company())
    monkeypatch.setattr(chiavi, "rivela_per_utente", lambda *args: "ok")
    monkeypatch.setattr(chiavi, "registra_uso", lambda *args, **kwargs: None)
    monkeypatch.setattr(chiavi, "saldo_per_utente", lambda uid: 497)
    token = explorer.reveal_token(UID_A, REF)

    response = portal_client().post("/portale/rivela-azienda", json={"reveal_token": token})
    assert response.status_code == 200
    assert response.json()["esito"] == "ok"
    assert response.json()["saldo"] == 497
    assert response.json()["azienda"]["company"] == COMPANY
    assert response.json()["azienda"]["domain"] == DOMAIN
    assert response.json()["azienda"]["logo"] == LOGO


def test_company_token_route_requires_reveal_and_returns_only_valid_credentials(
        monkeypatch, portal_client):
    monkeypatch.setattr(
        chiavi,
        "e_rivelata",
        lambda uid, kind, reference: reference == REF,
    )
    client = portal_client()

    unrevealed = client.post(
        "/portale/token-azienda", json={"company_ref": "greenhouse:not-revealed"})
    assert unrevealed.status_code == 402

    response = client.post("/portale/token-azienda", json={"company_ref": REF})
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"reveal_token", "reveal_group"}
    assert explorer.company_from_token(payload["reveal_token"], UID_A) == REF
    assert payload["reveal_group"] == explorer.reveal_group(REF)
    _assert_zero_leak(payload)


@pytest.fixture
def synthetic_duckdb(tmp_path):
    path = tmp_path / "explorer-security.duckdb"
    connection = duckdb.connect(str(path))
    connection.execute("""
        CREATE TABLE offerte (
            id VARCHAR,
            title VARCHAR,
            country VARCHAR,
            city VARCHAR,
            seniority VARCHAR,
            remote VARCHAR,
            category VARCHAR,
            posted_at TIMESTAMP,
            technologies VARCHAR[],
            ats VARCHAR,
            company VARCHAR,
            company_slug VARCHAR,
            raw VARCHAR
        )
    """)
    connection.execute("""
        CREATE TABLE aziende (
            ats VARCHAR,
            company_slug VARCHAR,
            company VARCHAR,
            country VARCHAR,
            industry VARCHAR,
            employees BIGINT,
            technologies VARCHAR[],
            raw VARCHAR
        )
    """)
    connection.execute(
        "CREATE TABLE flusso (t TIMESTAMP, id VARCHAR, event VARCHAR, raw VARCHAR)")

    secret = _secret_job()
    connection.execute(
        "INSERT INTO offerte VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            secret["id"], secret["title"], "IT", "Milano", "senior", "remote",
            "engineering", "2026-01-01 00:00:00", ["Python"], "greenhouse",
            COMPANY, SLUG, json.dumps({**secret, "company_domain": DOMAIN}),
        ],
    )
    connection.execute(
        "INSERT INTO offerte VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "public-posting-2", "Platform Engineer", "IT", "Roma", "mid", "onsite",
            "engineering", "2026-01-02 00:00:00", ["Python"], "greenhouse",
            "Public Example", "public-example",
            json.dumps({"description": "ordinary public description"}),
        ],
    )
    disjoint = {
        "id": "disjoint-domain-posting",
        "title": "ZqvxUnique Platform Engineer",
        "company": DISJOINT_COMPANY,
        "company_slug": DISJOINT_SLUG,
        "company_domain": DISJOINT_DOMAIN,
    }
    connection.execute(
        "INSERT INTO offerte VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            disjoint["id"], disjoint["title"], "IT", "Torino", "senior", "remote",
            "engineering", "2026-01-03 00:00:00", ["Python"], "greenhouse",
            DISJOINT_COMPANY, DISJOINT_SLUG, json.dumps(disjoint),
        ],
    )

    company = _secret_company()
    connection.execute(
        "INSERT INTO aziende VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "greenhouse", SLUG, COMPANY, "IT", "software", 123, ["Python"],
            json.dumps(company),
        ],
    )
    closed_resolved = {
        "id": "closed-resolved",
        "title": f"{COMPANY} | {BRAND} Closed Engineer",
        "ats": "greenhouse",
        "company_slug": SLUG,
        "url": APPLY,
        "country": "IT",
        "posted_at": "2026-01-01T00:00:00",
        "closed_at": "2026-01-04T00:00:00",
    }
    closed_unknown = {
        "id": "closed-unknown",
        "title": f"{COMPANY} | {BRAND} Unknown Employer",
        "ats": "greenhouse",
        "company_slug": "unknown-closed-company",
        "url": "https://unknown.example/apply",
        "country": "IT",
        "posted_at": "2026-01-02T00:00:00",
        "closed_at": "2026-01-05T00:00:00",
    }
    real_closed_fields = {
        "id", "title", "ats", "company_slug", "url", "country", "posted_at", "closed_at",
    }
    assert set(closed_resolved) == real_closed_fields
    assert set(closed_unknown) == real_closed_fields
    assert "company" not in closed_resolved and "domain" not in closed_resolved
    connection.execute(
        "INSERT INTO flusso VALUES (?, ?, 'closed', ?), (?, ?, 'closed', ?)",
        [
            "2026-01-04 00:00:00", closed_resolved["id"], json.dumps(closed_resolved),
            "2026-01-05 00:00:00", closed_unknown["id"], json.dumps(closed_unknown),
        ],
    )
    connection.close()

    previous_path = dati._percorso
    dati.apri(str(path))
    yield path
    if dati._con is not None:
        dati._con.close()
    dati._con = None
    dati._con_mtime = None
    dati._percorso = previous_path


@pytest.mark.parametrize("query", [COMPANY, BRAND, DESCRIPTION_MARKER])
def test_duckdb_preview_query_cannot_oracle_company_title_brand_or_description(
        synthetic_duckdb, query):
    rows, cursor, total = dati.cerca_portale({"q": query}, None, 20)
    assert rows == []
    assert cursor is None
    assert total == 0


@pytest.mark.parametrize("query", [DISJOINT_BRAND, DISJOINT_DOMAIN])
def test_duckdb_domain_brand_oracle_with_unrelated_legal_company(
        synthetic_duckdb, query):
    rows, cursor, total = dati.cerca_portale({"q": query}, None, 20)
    assert rows == []
    assert cursor is None
    assert total == 0


def test_duckdb_active_unknown_company_fails_closed_title_and_query(synthetic_duckdb):
    secret_title = "Unresolved Ghost Corp | Platform Engineer"
    with duckdb.connect(str(synthetic_duckdb)) as connection:
        connection.execute(
            "INSERT INTO offerte VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                "unknown-active", secret_title, "IT", None, None, None,
                "engineering", "2026-01-04 00:00:00", ["Python"], "greenhouse",
                None, "unknown-active-slug", json.dumps({"title": secret_title}),
            ],
        )
    rows, _, total = dati.cerca_portale({}, None, 20)
    row = next(row for row in rows if row["id"] == "unknown-active")
    dto = explorer.job_dto(row, False, "token", "group")
    assert dto["title"] == "Hidden company"
    assert secret_title not in json.dumps(dto)
    assert "unknown-active-slug" not in json.dumps(dto)
    assert dati.cerca_portale({"q": "Unresolved Ghost Corp"}, None, 20)[2] == 0
    assert total >= 1


def test_duckdb_preview_still_finds_redacted_public_title(synthetic_duckdb):
    rows, _, total = dati.cerca_portale({"q": "Platform Engineer"}, None, 20)
    assert total == 3
    assert {row["id"] for row in rows} == {
        "public-posting-1", "public-posting-2", "disjoint-domain-posting",
    }
    secret_row = next(row for row in rows if row["id"] == "public-posting-1")
    reference = f"{secret_row['ats']}:{secret_row['company_slug']}"
    profile = dati.profili_aziende([reference])[reference]
    _assert_zero_leak(explorer.job_dto(secret_row, False, "token", "group", profile))


def test_duckdb_closed_real_shape_resolves_company_and_redacts_unknown(
        synthetic_duckdb):
    rows, cursor, total = dati.cerca_chiuse_portale({}, None, 20)
    assert total == 2
    assert cursor is None
    by_id = {row["id"]: row for row in rows}
    assert set(by_id) == {"closed-resolved", "closed-unknown"}

    resolved = by_id["closed-resolved"]
    assert resolved["company"] == COMPANY
    assert resolved["company_domain"] == DOMAIN
    assert resolved["title_requires_redaction"] is False
    resolved_dto = explorer.job_dto(resolved, False, "token", "group")
    _assert_zero_leak(resolved_dto)
    assert "Hidden company" in resolved_dto["title"]

    unknown = by_id["closed-unknown"]
    assert unknown["company"] is None
    assert unknown["company_domain"] is None
    assert unknown["title_requires_redaction"] is True
    unknown_dto = explorer.job_dto(unknown, False, "token", "group")
    _assert_zero_leak(unknown_dto)
    assert unknown_dto["title"] == "Hidden company"


@pytest.mark.parametrize("query", [COMPANY, BRAND])
def test_duckdb_closed_company_and_brand_queries_return_no_rows(
        synthetic_duckdb, query):
    rows, cursor, total = dati.cerca_chiuse_portale({"q": query}, None, 20)
    assert rows == []
    assert cursor is None
    assert total == 0


def _require_local_test_dsn(dsn: str) -> None:
    if dsn.startswith(("postgresql://", "postgres://")):
        host = urlsplit(dsn).hostname
    else:
        host = psycopg.conninfo.conninfo_to_dict(dsn).get("host", "")
    assert host in (None, "", "localhost", "127.0.0.1", "::1") or host.startswith("/"), (
        "TEST_DATABASE_URL must point to local PostgreSQL"
    )


@pytest.fixture
def pg_accounts(monkeypatch):
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL concurrency tests")
    _require_local_test_dsn(dsn)

    previous_pool = chiavi._pool
    chiavi.configura_pool(None)
    monkeypatch.setattr(chiavi, "_URL", dsn)
    created = []

    def create(balance: int = 3) -> str:
        uid = str(uuid.uuid4())
        key_id = uuid.uuid4()
        created.append(uid)
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "INSERT INTO users "
                "(id, email, plan, subscription_status, frequency, crediti_extra) "
                "VALUES (%s, %s, 'basic', 'trialing', 'daily', 0)",
                (uid, f"explorer-{uid}@example.test"),
            )
            connection.execute(
                "INSERT INTO api_chiavi "
                "(id, key_hash, label, crediti_mensili, usati_mese, user_id, key_tail) "
                "VALUES (%s, %s, 'Explorer security test', %s, 0, %s, 'sec001')",
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


def _concurrent_reveals(uid: str, references: list[str]) -> list[tuple[str, str]]:
    barrier = threading.Barrier(len(references))

    def reveal(reference: str) -> tuple[str, str]:
        barrier.wait(timeout=15)
        return reference, chiavi.rivela_per_utente(uid, "azienda", reference)

    with ThreadPoolExecutor(max_workers=len(references)) as pool:
        futures = [pool.submit(reveal, reference) for reference in references]
        return [future.result(timeout=45) for future in futures]


def _billing_state(dsn: str, uid: str) -> tuple[int, int, int, int, set[str]]:
    with psycopg.connect(dsn) as connection:
        used, extra = connection.execute(
            "SELECT k.usati_mese, u.crediti_extra "
            "FROM users u JOIN api_chiavi k ON k.user_id = u.id WHERE u.id = %s",
            (uid,),
        ).fetchone()
        ledger_count, ledger_credits = connection.execute(
            "SELECT count(*), coalesce(sum(crediti), 0) FROM portale_uso "
            "WHERE user_id = %s AND via = 'portale' AND azione = 'company unlock'",
            (uid,),
        ).fetchone()
        unlocked = {
            row[0]
            for row in connection.execute(
                "SELECT riferimento FROM portale_rivelazioni "
                "WHERE user_id = %s AND tipo = 'azienda'",
                (uid,),
            ).fetchall()
        }
    return used, extra, ledger_count, ledger_credits, unlocked


def test_postgres_concurrent_distinct_companies_charge_exactly_one(
        pg_accounts):
    dsn = os.environ["TEST_DATABASE_URL"]
    uid = pg_accounts(balance=3)
    references = [f"greenhouse:distinct-{uuid.uuid4()}-{index}" for index in range(8)]

    outcomes = _concurrent_reveals(uid, references)
    assert Counter(outcome for _, outcome in outcomes) == Counter({
        "ok": 1,
        "senza_crediti": len(references) - 1,
    })
    paid_reference = next(reference for reference, outcome in outcomes if outcome == "ok")
    used, extra, ledger_count, ledger_credits, unlocked = _billing_state(dsn, uid)

    assert (used, extra) == (chiavi.COSTO_COMPANY, 0)
    assert (ledger_count, ledger_credits) == (1, chiavi.COSTO_COMPANY)
    assert unlocked == {paid_reference}
    assert chiavi.saldo_per_utente(uid) == 0


def test_postgres_same_company_concurrency_is_one_charge_then_gratis(
        pg_accounts):
    dsn = os.environ["TEST_DATABASE_URL"]
    uid = pg_accounts(balance=3)
    reference = f"greenhouse:same-{uuid.uuid4()}"
    references = [reference] * 8

    outcomes = _concurrent_reveals(uid, references)
    assert Counter(outcome for _, outcome in outcomes) == Counter({"ok": 1, "gratis": 7})
    before = _billing_state(dsn, uid)
    assert before[:4] == (chiavi.COSTO_COMPANY, 0, 1, chiavi.COSTO_COMPANY)
    assert before[4] == {reference}

    assert chiavi.rivela_per_utente(uid, "azienda", reference) == "gratis"
    assert _billing_state(dsn, uid) == before
    assert chiavi.saldo_per_utente(uid) == 0
    assert chiavi.e_rivelata(uid, "azienda", reference) is True
    assert chiavi.rivelate_per_utente(uid, "azienda", [reference]) == {reference}


def test_postgres_full_http_reveal_has_one_three_credit_ledger_row(
        pg_accounts, portal_client, monkeypatch):
    dsn = os.environ["TEST_DATABASE_URL"]
    uid = pg_accounts(balance=3)
    slug = f"http-reveal-{uuid.uuid4()}"
    reference = f"greenhouse:{slug}"
    company = {**_secret_company(), "company_slug": slug, "ref": reference}
    monkeypatch.setattr(
        dati,
        "rivela_azienda",
        lambda requested: company if requested == reference else None,
    )
    token = explorer.reveal_token(uid, reference)
    client = portal_client(uid)

    first = client.post("/portale/rivela-azienda", json={"reveal_token": token})
    assert first.status_code == 200
    assert first.json()["esito"] == "ok"
    assert first.json()["saldo"] == 0
    before = _billing_state(dsn, uid)
    assert before[:4] == (chiavi.COSTO_COMPANY, 0, 1, chiavi.COSTO_COMPANY)
    assert before[4] == {reference}
    assert chiavi.saldo_per_utente(uid) == 0
    assert chiavi.conta_rivelazioni(uid) == 1

    second = client.post("/portale/rivela-azienda", json={"reveal_token": token})
    assert second.status_code == 200
    assert second.json()["esito"] == "gratis"
    assert second.json()["saldo"] == 0
    assert _billing_state(dsn, uid) == before
