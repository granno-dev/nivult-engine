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
    "company", "company_id", "company_slug", "company_domain", "domain", "logo",
    "logo_url", "website", "url", "source_url", "apply", "apply_url",
    "application_url", "source", "source_type", "ats", "description",
    "description_preview", "summary", "summary_source", "benefits", "about",
    "external_id", "job_sources", "ref", "city", "location", "remote", "skills",
    "language", "languages_required", "employees", "employee_range", "company_size",
    "n_tecnologie", "open_jobs_count", "active_jobs", "jobs_posted_30d", "n_locations",
}
JOB_PREVIEW_KEYS = {
    "id", "title", "category", "seniority", "work_mode", "posted_at", "country",
    "employment_type", "source_label", "technologies", "company_size_band", "industry",
    "active_postings_count", "posted_30d_count", "languages", "is_aggregator",
    "revealed", "az_rivelata", "job_revealed", "reveal_token", "reveal_group",
}
COMPANY_PREVIEW_KEYS = {
    "country", "industry", "company_size_band", "active_postings_count",
    "posted_30d_count", "technologies", "languages", "is_aggregator", "revealed",
    "reveal_token", "reveal_group",
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
        "employment_type": "full_time",
        "languages_required": ["en", "it"],
        "technologies": ["Python", "Excel"],
        "employee_range": "100-249",
        "industry": "software",
        "active_jobs": 7,
        "jobs_posted_30d": 3,
        "benefits": ["confidential benefit"],
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
        "jobs_posted_30d": 3,
        "languages": ["en"],
        "technologies": [{"technology": "Python", "active_jobs": 4}, "Excel"],
        "locations": [{"city": "Milano", "country": "IT"}],
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
    assert payload["source_url"] == APPLY
    assert payload["source_type"] == SOURCE
    assert payload["description"] == DESCRIPTION
    assert payload["technologies"] == [
        {"name": "Python", "group": "core"},
        {"name": "Excel", "group": "productivity"},
    ]


def _assert_revealed_company(payload: dict) -> None:
    assert payload["revealed"] is True
    assert payload["company"] == COMPANY
    assert payload["name"] == COMPANY
    assert payload["company_slug"] == SLUG
    assert payload["domain"] == DOMAIN
    assert payload["logo"] == LOGO
    assert payload["website"] == DOMAIN
    assert payload["active_jobs"] == 7
    assert payload["company_size"] == "100-249"
    assert payload["about"] == DESCRIPTION
    assert payload["locations_count"] == 1
    assert payload["technologies"] == [
        {"name": "Python", "group": "core"},
        {"name": "Excel", "group": "productivity"},
    ]


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
    assert set(preview) <= (COMPANY_PREVIEW_KEYS if kind == "company" else JOB_PREVIEW_KEYS)
    if kind != "company":
        assert "Hidden company" in preview["title"]
        assert preview["source_label"] == "Greenhouse"
        assert preview["work_mode"] == "remote"
        assert preview["employment_type"] == "full_time"
        assert preview["languages"] == ["en", "it"]
        assert preview["active_postings_count"] == 7
        assert preview["posted_30d_count"] == 3
        assert preview["company_size_band"] == "100-249"
        assert preview["technologies"][1]["group"] == "productivity"
    else:
        assert preview["active_postings_count"] == 7
        assert preview["posted_30d_count"] == 3
        assert preview["company_size_band"] == "100-249"

    revealed = dto(row, True, "opaque-token", "opaque-group")
    if kind == "company":
        _assert_revealed_company(revealed)
    else:
        _assert_revealed_job(revealed)


@pytest.mark.parametrize(("ats", "label"), [
    ("jsonld", "Direct Career Site"),
    ("icims", "iCIMS"),
    ("lever", "Lever"),
    ("successfactors", "SuccessFactors"),
    ("zoho", "Zoho Recruit"),
    ("zoho_recruit_eu", "Zoho Recruit"),
])
def test_structured_source_label_mapping(ats, label):
    row = {**_secret_job(), "ats": ats}
    preview = explorer.job_dto(row, False, "token", "group")
    assert preview["source_label"] == label
    assert "ats" not in preview
    _assert_zero_leak(preview)


def test_prefix_brand_franchise_and_compact_identity_redaction():
    assert explorer.clean_company_name("Careers Marinclinic") == "Marinclinic"
    assert explorer.clean_company_name("Jobs at Marinclinic") == "Marinclinic"
    assert explorer.clean_company_name("Hiring for Marinclinic") == "Marinclinic"
    for company in ("Careers Marinclinic", "Jobs at Marinclinic", "Hiring Marinclinic"):
        assert explorer.safe_title("Marinclinic | Nurse", company) == "Hidden company | Nurse"
    for variant in ("Chick-fil-A", "chick fil a", "chickfila"):
        title = explorer.safe_title(f"{variant} | Team Member", "Chick-fil-A Franchise")
        assert variant.casefold() not in title.casefold()
        assert "Hidden company" in title
    revealed = explorer.company_dto(
        {**_secret_company(), "company": "Careers Marinclinic"},
        True, "token", "group")
    assert revealed["company"] == revealed["name"] == "Marinclinic"


def test_locked_dto_has_no_prose_or_marin_identity_and_groups_generic_tech():
    row = {
        **_secret_job(),
        "company": "Marin Community Clinics",
        "title": "MarinCommunityClinics | Registered Nurse",
        "description": "Marin Community Clinics confidential nursing prose",
        "summary": "Marin Community Clinics summary",
        "benefits": ["Marin Community Clinics benefits"],
        "technologies": ["Python", "Excel", "Word", "Outlook", "PowerPoint",
                         "Microsoft Office", "Google Workspace"],
    }
    preview = explorer.detail_dto(row, False, "token", "group")
    _assert_zero_leak(preview)
    serialized = json.dumps(preview)
    assert "Marin Community Clinics" not in serialized
    assert "MarinCommunityClinics" not in serialized
    assert "description_preview" not in preview
    assert {tech["group"] for tech in preview["technologies"][1:]} == {"productivity"}
    assert preview["technologies"][0] == {"name": "Python", "group": "core"}


@pytest.mark.parametrize("domain", ["jobgether.com", "https://justjoin.it/jobs/123"])
def test_known_board_intermediary_is_flagged_without_exposing_domain(domain):
    row = {**_secret_job(), "source_type": "direct", "company_domain": domain}
    preview = explorer.job_dto(row, False, "token", "group")
    assert preview["is_aggregator"] is True
    assert "company_domain" not in preview
    assert domain not in json.dumps(preview)


def test_company_profile_prose_is_never_merged_into_job():
    row = _secret_job()
    del row["description"]
    del row["industry"]
    del row["active_jobs"]
    profile = {**_secret_profile(), "description": "Company profile prose",
               "industry": "healthcare", "active_jobs": 11}
    revealed = explorer.job_dto(row, True, "token", "group", profile)
    assert "description" not in revealed
    assert revealed["industry"] == "healthcare"
    assert revealed["active_postings_count"] == 11
    legacy = explorer.job_dto(row, False, "token", "group", profile, job_revealed=True)
    assert "description" not in legacy


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


def test_duckdb_company_preview_populates_only_safe_structured_signals(synthetic_duckdb):
    rows, _, total = dati.cerca_aziende_portale({}, None, 20)
    assert total == 1
    preview = explorer.company_dto(rows[0], False, "token", "group")
    _assert_zero_leak(preview)
    assert set(preview) <= COMPANY_PREVIEW_KEYS
    assert preview["active_postings_count"] == 7
    assert preview["posted_30d_count"] == 3
    assert preview["company_size_band"] == "100-249"
    assert preview["languages"] == ["en"]
    assert preview["technologies"] == [{"name": "Python", "group": "core"}]


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


def test_locked_detail_has_no_prose_and_legacy_text_is_complete_but_sanitized():
    row = _secret_job()
    row["description"] = (
        "<p>Zephyr Quantum Labs is hiring. Visit https://zephyrquantum.example/jobs "
        "or email hiring@zephyrquantum.example. Phone: +39 02 1234 5678.</p>"
        "<p>Internal slug zephyr-quantum-7f3e-secret. Telefono: 02 1234 5678. "
        "Logo: //cdn.example.net/logo.svg</p>"
        "<p>You will build reliable data platforms with Python.</p>")
    preview = explorer.detail_dto(row, False, "token", "group")
    _assert_zero_leak(preview)
    assert "description" not in preview and "description_preview" not in preview
    assert preview["job_revealed"] is False

    full = explorer.detail_dto(row, False, "token", "group", job_revealed=True)["description"]
    assert "You will build reliable data platforms with Python." in full
    for secret in (COMPANY, BRAND, SLUG, "hiring@zephyrquantum.example",
                   "+39 02 1234 5678", "02 1234 5678", "cdn.example.net"):
        assert secret not in full


def test_legacy_job_unlock_http_is_permanent_readable_and_never_charges(
        monkeypatch, portal_client):
    _stub_projection(monkeypatch, set())
    monkeypatch.setattr(
        chiavi, "e_rivelata",
        lambda uid, kind, reference: kind == "job" and reference == "public-posting-1")

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy reads must not charge or write usage")

    monkeypatch.setattr(chiavi, "rivela_per_utente", forbidden)
    monkeypatch.setattr(chiavi, "registra_uso", forbidden)
    monkeypatch.setattr(chiavi, "saldo_per_utente", forbidden)
    response = portal_client().post(
        "/portale/offerta-dettaglio", json={"id": "public-posting-1"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["job_rivelata"] is True and payload["azienda_rivelata"] is False
    description = payload["offerta"]["description"]
    assert DESCRIPTION_MARKER in description
    assert COMPANY not in description
    assert "company" not in payload["offerta"] and "url" not in payload["offerta"]
    assert "description_preview" not in payload["offerta"]
    serialized = json.dumps(payload)
    for secret in SECRET_VALUES - {DESCRIPTION, DESCRIPTION_MARKER}:
        assert secret not in serialized


def test_job_unlock_route_returns_410_before_data_quota_or_charging(
        monkeypatch, portal_client):
    def forbidden(*args, **kwargs):
        raise AssertionError("retired job unlock must not reach any backend")

    for module, name in (
            (chiavi, "consuma_quota_explorer"), (chiavi, "rivela_per_utente"),
            (chiavi, "registra_uso"), (chiavi, "saldo_per_utente"),
            (vivo, "rivela_offerta")):
        monkeypatch.setattr(module, name, forbidden)
    response = portal_client().post("/portale/rivela", json={"id": "public-posting-1"})
    assert response.status_code == 410
    assert response.headers["Cache-Control"] == "no-store"


def test_revealed_source_url_is_added_only_for_valid_http_urls():
    valid = explorer.job_dto(_secret_job(), True, "token", "group")
    assert valid["source_url"] == APPLY
    invalid = explorer.job_dto(
        {**_secret_job(), "url": "javascript:alert(1)"}, True, "token", "group")
    assert "source_url" not in invalid
    assert invalid["url"] == "javascript:alert(1)"


def test_vivo_title_enrichment_adds_indexed_source_type(monkeypatch):
    job_id = str(uuid.uuid4())

    class Connection:
        def execute(self, sql, params):
            assert "LEFT JOIN ats_companies" in sql
            assert "COALESCE(co.source_type, 'direct')" in sql
            assert params == {"ids": [job_id]}
            return self

        def fetchall(self):
            return [{"id": job_id, "url": None, "source_type": "aggregator"}]

    from contextlib import contextmanager

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(vivo, "_conn", connection)
    rows = [{"id": job_id, "title": "Engineer", "ats": "jsonld",
             "company_slug": "jobgether.com"}]
    assert vivo.completa_titoli_portale(rows)[0]["source_type"] == "aggregator"


def test_scoped_aggregator_reveals_only_selected_employer_and_jobs(
        synthetic_duckdb, monkeypatch, portal_client):
    slug = "jobgether.com"
    base_ref = f"jsonld:{slug}"
    employers = {"agg-a": "Chick-fil-A Franchise", "agg-b": "Marin Community Clinics"}
    rows = []
    with duckdb.connect(str(synthetic_duckdb)) as connection:
        for index, (job_id, company) in enumerate(employers.items(), start=1):
            row = {
                "id": job_id, "title": f"{company} | Team Member", "ats": "jsonld",
                "country": "US", "city": "Austin", "seniority": "mid", "remote": "onsite",
                "category": "hospitality", "posted_at": f"2026-01-0{index}T00:00:00",
                "employment_type": "full_time", "languages_required": ["en"],
                "technologies": ["Excel"], "company": company, "company_slug": slug,
                "company_domain": "jobgether.com", "source_type": "aggregator",
                "description": f"{company} private prose", "url": f"https://jobs.example/{job_id}",
            }
            rows.append(row)
            connection.execute(
                "INSERT INTO offerte VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [job_id, row["title"], "US", "Austin", "mid", "onsite", "hospitality",
                 f"2026-01-0{index} 00:00:00", ["Excel"], "jsonld", company, slug,
                 json.dumps(row)])

        connection.execute(
            "INSERT INTO aziende VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ["jsonld", slug, "Jobgether", "US", "jobs", 100, ["Excel"],
             json.dumps({"company": "Jobgether", "domain": "jobgether.com"})],
        )

    refs = {job_id: f"{base_ref}::employer:{explorer.employer_key(company)}"
            for job_id, company in employers.items()}
    assert dati.rivela_azienda(refs["agg-a"])["company"] == "Chick-fil-A Franchise"
    assert dati.rivela_azienda(refs["agg-b"])["company"] == "Marin Community Clinics"
    assert dati.rivela_azienda(base_ref + "::employer:unknownemployer") is None
    profiles = dati.profili_aziende(list(refs.values()))
    assert profiles[refs["agg-a"]]["company"] == "Chick-fil-A Franchise"
    assert profiles[refs["agg-b"]]["company"] == "Marin Community Clinics"
    assert [row["id"] for row in dati.azienda_jobs_portale(refs["agg-a"])] == ["agg-a"]
    assert [row["id"] for row in dati.azienda_jobs_portale(refs["agg-b"])] == ["agg-b"]

    revealed = {refs["agg-a"]}
    monkeypatch.setattr(dati, "cerca_portale", lambda *args: (rows, None, 2))
    monkeypatch.setattr(vivo, "completa_titoli_portale", lambda values: values)
    monkeypatch.setattr(chiavi, "rivelate_per_utente",
                        lambda uid, kind, references: set(references) & revealed)
    monkeypatch.setattr(chiavi, "e_rivelata",
                        lambda uid, kind, reference: reference in revealed)
    client = portal_client()
    response = client.post("/portale/cerca", json={})
    assert response.status_code == 200
    by_id = {row["id"]: row for row in response.json()["data"]}
    assert by_id["agg-a"]["revealed"] is True
    assert by_id["agg-a"]["company"] == "Chick-fil-A Franchise"
    assert by_id["agg-b"]["revealed"] is False
    assert "company" not in by_id["agg-b"]
    assert "Marin Community Clinics" not in json.dumps(by_id["agg-b"])
    assert by_id["agg-b"]["is_aggregator"] is True
    assert "company_domain" not in by_id["agg-b"]

    revealed.clear()
    revealed.add(base_ref)
    legacy_broad = client.post("/portale/cerca", json={}).json()["data"]
    assert all(row["revealed"] is False for row in legacy_broad)

    revealed.clear()
    revealed.add(refs["agg-a"])
    monkeypatch.setattr(chiavi, "rivela_per_utente", lambda *args: "ok")
    monkeypatch.setattr(chiavi, "registra_uso", lambda *args, **kwargs: None)
    monkeypatch.setattr(chiavi, "saldo_per_utente", lambda uid: 497)
    token = explorer.reveal_token(UID_A, refs["agg-a"])
    scoped_reveal = client.post("/portale/rivela-azienda", json={"reveal_token": token})
    assert scoped_reveal.status_code == 200
    assert scoped_reveal.json()["azienda"]["company_ref"] == refs["agg-a"]
    monkeypatch.setattr(vivo, "rivela_offerta", lambda offer_id: rows[0])
    detail = client.post("/portale/offerta-dettaglio", json={"id": "agg-a"})
    assert detail.status_code == 200
    assert detail.json()["company_ref"] == refs["agg-a"]
    jobs = client.post("/portale/azienda-jobs", json={"reveal_token": token})
    assert jobs.status_code == 200
    assert [row["id"] for row in jobs.json()["data"]] == ["agg-a"]
    assert {"id", "title", "city", "posted_at"} <= set(jobs.json()["data"][0])
    assert "Marin Community Clinics" not in json.dumps(jobs.json())

    revealed.clear()
    revealed.add(base_ref)

    def no_charge(*args):
        raise AssertionError("a board-wide profile must never be charged")

    monkeypatch.setattr(chiavi, "rivela_per_utente", no_charge)
    assert dati.rivela_azienda(base_ref).get("source_type") is None
    broad_token = explorer.reveal_token(UID_A, base_ref)
    broad_reveal = client.post("/portale/rivela-azienda", json={"reveal_token": broad_token})
    assert broad_reveal.status_code == 409
    broad_jobs = client.post("/portale/azienda-jobs", json={"reveal_token": broad_token})
    assert broad_jobs.status_code == 409
    constants = dati.azienda_jobs_portale.__code__.co_consts
    assert any("lower(company) = lower($n)" in str(value) and "LIMIT 50" in str(value)
               for value in constants)


def test_postgres_new_job_unlock_is_rejected_and_legacy_rows_stay_readable(pg_accounts):
    dsn = os.environ["TEST_DATABASE_URL"]
    uid = pg_accounts(balance=3)
    reference = f"job-unlock-{uuid.uuid4()}"
    assert chiavi.rivela_per_utente(uid, "job", reference) == "non_disponibile"
    assert chiavi.rivela_per_utente(uid, "job", reference) == "non_disponibile"
    assert chiavi.saldo_per_utente(uid) == 3
    with psycopg.connect(dsn) as connection:
        charged = connection.execute(
            "SELECT count(*) FROM portale_uso WHERE user_id = %s "
            "AND azione = 'posting unlock'", (uid,)).fetchone()[0]
        assert charged == 0
        connection.execute(
            "INSERT INTO portale_rivelazioni (user_id, tipo, riferimento) "
            "VALUES (%s, 'job', %s)", (uid, reference))
    assert chiavi.e_rivelata(uid, "job", reference) is True
    assert chiavi.saldo_per_utente(uid) == 3
