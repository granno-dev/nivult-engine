from __future__ import annotations

import gzip
import json
import os
import uuid
from urllib.parse import urlsplit

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from nivult.api import app as app_module
from nivult.api_clienti import chiavi, dati, opensearch, v1, vivo


def _client(monkeypatch, *, saldo=100_000, quota=500_000):
    cliente = {
        "id": "key-hardening", "user_id": "user-hardening", "monthly_tier": quota,
        "crediti_residui": saldo, "subscription_status": "active",
        "creem_subscription_id": "sub", "current_period_end": None,
    }
    app = FastAPI()
    app.middleware("http")(v1.billing_rapidapi)
    app.include_router(v1.router)
    app.dependency_overrides[v1._cliente] = lambda: cliente
    monkeypatch.setattr(chiavi, "registra_servizio_chiave", lambda _: {})
    return TestClient(app)


def _export_state(tmp_path):
    paths = {}
    for name in ("offerte", "aziende"):
        path = tmp_path / f"{name}-2026-11-27.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as output:
            output.write(json.dumps({"id": name, "recruiter": "private"}) + "\n")
        paths[name] = str(path)
    return {
        "stato": "ok", "data_export": "2026-11-27", "offerte": 1, "aziende": 1,
        "flusso": 0, "flusso_giorni": 7, "file_offerte": paths["offerte"],
        "file_aziende": paths["aziende"], "generato_at": "2026-11-27T05:30:00+00:00",
        "data": "2026-11-27", "righe": {"offerte": 1, "aziende": 1, "flusso": 0},
        "file": paths,
    }


def test_public_manifest_has_only_api_paths_and_internal_state_is_unchanged(
        monkeypatch, tmp_path):
    state = _export_state(tmp_path)
    monkeypatch.setattr(dati, "stato_export", lambda: state)
    response = _client(monkeypatch).get("/v1/exports/latest")

    assert response.status_code == 200
    payload = response.json()
    serialized = json.dumps(payload)
    for prefix in ("/var/", "/opt/", "/home/", "/tmp/"):
        assert prefix not in serialized
    assert "file_offerte" not in payload and "file_aziende" not in payload
    assert payload["file"] == {
        "offerte": "/v1/exports/latest/offerte",
        "aziende": "/v1/exports/latest/aziende",
    }
    assert state["file_offerte"].startswith(str(tmp_path))
    assert state["file"]["offerte"].startswith(str(tmp_path))

    download = _client(monkeypatch).get("/v1/exports/latest/jobs")
    assert download.status_code == 200
    portal_path = dati.stato_export()["file"]["offerte"]
    assert portal_path == state["file_offerte"]
    assert os.path.isfile(portal_path)
    with gzip.open(portal_path, "rt", encoding="utf-8") as portal_export:
        assert json.loads(portal_export.readline())["id"] == "offerte"


@pytest.mark.parametrize(("requested", "canonical", "filename"), [
    ("offerte", "offerte", "nivult-jobs.jsonl.gz"),
    ("jobs", "offerte", "nivult-jobs.jsonl.gz"),
    ("aziende", "aziende", "nivult-companies.jsonl.gz"),
    ("companies", "aziende", "nivult-companies.jsonl.gz"),
])
def test_export_english_aliases_and_italian_names_stream_the_same_sanitized_file(
        monkeypatch, tmp_path, requested, canonical, filename):
    monkeypatch.setattr(dati, "stato_export", lambda: _export_state(tmp_path))
    response = _client(monkeypatch).get(f"/v1/exports/latest/{requested}")

    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == f'attachment; filename="{filename}"'
    assert response.headers["X-Credits-Charged"] == "0"
    assert [json.loads(line) for line in gzip.decompress(response.content).splitlines()] == [
        {"id": canonical}
    ]


def test_export_aliases_keep_scale_entitlement(monkeypatch, tmp_path):
    monkeypatch.setattr(dati, "stato_export", lambda: _export_state(tmp_path))
    client = _client(monkeypatch, quota=100_000)
    for name in ("offerte", "jobs", "aziende", "companies"):
        assert client.get(f"/v1/exports/latest/{name}").status_code == 403


def test_paid_routes_report_actual_credits_on_results_and_empty_results(monkeypatch):
    job_rows = [{"id": "job-1"}, {"id": "job-2"}]
    company_rows = [{"company": "Acme"}, {"company": "Beta"}]
    change_rows = [{"id": "change-1"}, {"id": "change-2"}]
    monkeypatch.setattr(opensearch, "attivo", lambda: True)
    monkeypatch.setattr(opensearch, "offerte",
                        lambda filters, cursor, limit: (job_rows[:limit], None))
    monkeypatch.setattr(dati, "aziende",
                        lambda filters, cursor, limit: (company_rows[:limit], None))
    monkeypatch.setattr(dati, "azienda",
                        lambda platform, slug: {"company": "Acme"} if slug == "acme" else None)
    monkeypatch.setattr(dati, "cambiamenti",
                        lambda since, cursor, limit: (change_rows[:limit], None))
    monkeypatch.setattr(chiavi, "addebita_chiave", lambda *args, **kwargs: {})
    client = _client(monkeypatch)

    cases = [
        ("/v1/jobs", 200, "2"),
        ("/v1/companies", 200, "6"),
        ("/v1/companies/greenhouse/acme", 200, "3"),
        ("/v1/changes?since=2026-11-24T00:00:00Z", 200, "2"),
    ]
    for path, status, charged in cases:
        response = client.get(path)
        assert response.status_code == status
        assert response.headers["X-Credits-Charged"] == charged

    job_rows.clear()
    company_rows.clear()
    change_rows.clear()
    empty_cases = [
        ("/v1/jobs", 200),
        ("/v1/companies", 200),
        ("/v1/companies/greenhouse/missing", 404),
        ("/v1/changes?since=2026-11-24T00:00:00Z", 200),
    ]
    for path, status in empty_cases:
        response = client.get(path)
        assert response.status_code == status
        assert response.headers["X-Credits-Charged"] == "0"


def test_paid_routes_report_zero_on_managed_errors_and_keep_credit_429(monkeypatch):
    monkeypatch.setattr(opensearch, "attivo", lambda: True)
    monkeypatch.setattr(opensearch, "offerte", lambda *args: ([], None))
    monkeypatch.setattr(chiavi, "addebita_chiave", lambda *args, **kwargs: {})
    assert _client(monkeypatch).get("/v1/jobs?salary_min=bad").status_code == 400
    response = _client(monkeypatch).get("/v1/jobs?salary_min=bad")
    assert response.headers["X-Credits-Charged"] == "0"
    validation = _client(monkeypatch).get("/v1/jobs?limit=0")
    assert validation.status_code == 422
    assert validation.headers["X-Credits-Charged"] == "0"
    assert isinstance(validation.json()["detail"], list)

    def unavailable(*args):
        raise psycopg.OperationalError("synthetic outage")

    monkeypatch.setattr(opensearch, "offerte", unavailable)
    unavailable_response = _client(monkeypatch).get("/v1/jobs")
    assert unavailable_response.status_code == 503
    assert unavailable_response.headers["X-Credits-Charged"] == "0"

    exhausted = _client(monkeypatch, saldo=0).get("/v1/jobs")
    assert exhausted.status_code == 429
    assert exhausted.json()["detail"]["error"] == "insufficient credits for one result"
    assert exhausted.headers["X-Credits-Charged"] == "0"


def test_cors_exposes_credits_charged(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://portal.test")
    monkeypatch.setattr(opensearch, "attivo", lambda: True)
    monkeypatch.setattr(opensearch, "offerte", lambda *args: ([{"id": "job"}], None))
    monkeypatch.setattr(chiavi, "addebita_chiave", lambda *args, **kwargs: {})
    application = app_module.create_app()
    application.dependency_overrides[v1._cliente] = lambda: {
        "id": "key", "user_id": "user", "monthly_tier": 500_000,
        "crediti_residui": 100, "subscription_status": "active",
        "creem_subscription_id": "sub", "current_period_end": None,
    }
    response = TestClient(application).get("/v1/jobs", headers={"Origin": "https://portal.test"})
    assert response.status_code == 200
    assert response.headers["X-Credits-Charged"] == "1"
    assert "X-Credits-Charged" in response.headers["Access-Control-Expose-Headers"]


def test_bundesagentur_alias_is_normalized_token_by_token_in_both_parameters():
    filters = v1._filtri(
        ats="greenhouse,Bundesagentur,bUnDeSaGeNtUr",
        exclude_ats="jsonld,BUNDESAGENTUR",
    )
    assert filters["ats"] == "greenhouse,bundesanstellung,bundesanstellung"
    assert filters["exclude_ats"] == "jsonld,bundesanstellung"


@pytest.mark.parametrize(("query", "expected"), [
    ("employees_min=-1", "employees_min must be a non-negative integer"),
    ("employees_max=-1", "employees_max must be a non-negative integer"),
    ("employees_min=10&employees_max=9", "employees_min cannot exceed employees_max"),
    ("employees_min=not-an-int", "employees_min must be an integer"),
])
def test_employees_range_validation_uses_salary_error_contract(monkeypatch, query, expected):
    called = []
    monkeypatch.setattr(dati, "aziende", lambda *args: called.append(args) or ([], None))
    response = _client(monkeypatch).get(f"/v1/companies?{query}")
    assert response.status_code == 400
    assert response.json() == {"detail": expected}
    assert response.headers["X-Credits-Charged"] == "0"
    assert called == []


def test_employees_range_accepts_zero_and_passes_integers(monkeypatch):
    captured = {}

    def query(filters, cursor, limit):
        captured.update(filters)
        return [], None

    monkeypatch.setattr(dati, "aziende", query)
    response = _client(monkeypatch).get("/v1/companies?employees_min=0&employees_max=10")
    assert response.status_code == 200
    assert response.headers["X-Credits-Charged"] == "0"
    assert captured["employees_min"] == 0 and captured["employees_max"] == 10


def _local_test_dsn():
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for real PostgreSQL hardening tests")
    if dsn.startswith(("postgresql://", "postgres://")):
        host = urlsplit(dsn).hostname
    else:
        host = psycopg.conninfo.conninfo_to_dict(dsn).get("host", "")
    assert host in (None, "", "localhost", "127.0.0.1", "::1") or host.startswith("/")
    return dsn


@pytest.fixture
def pg_vivo(monkeypatch):
    dsn = _local_test_dsn()
    schema = f"hardening_{uuid.uuid4().hex}"
    connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    connection.execute(f'CREATE SCHEMA "{schema}"')
    connection.execute(f'SET search_path TO "{schema}", public')
    connection.execute("""
        CREATE TABLE ats_jobs (
            id uuid PRIMARY KEY, title text, platform_id text, slug text, url text,
            country text, city text, location text, lang text, seniority text, remote text,
            skills text[], salary_min numeric, salary_max numeric, salary_currency text,
            posted_at timestamptz, created_at timestamptz, fetched_at timestamptz,
            raw jsonb, employment_type text, languages_required text[], external_id text,
            department text, orario text, durata text, salary_period text, salary_da text,
            posted_at_estimated boolean, content_updated_at timestamptz,
            duplicate_key text, expired_at timestamptz
        );
        CREATE TABLE ats_companies (
            platform_id text, slug text, company_name text, source_type text, site_domain text
        );
        CREATE TABLE job_classifications (job_id uuid, family text);
        CREATE TABLE tecnologie_v1 (job_id uuid, tecnologie text[]);
        CREATE TABLE sintesi_finali (job_id uuid, sintesi text, da text);
        CREATE TABLE offerte_dettagli (
            job_id uuid, management_level text, is_decision_maker boolean,
            shift_schedule text, work_hours text, is_urgently_hiring boolean,
            benefits text[], salary_text text, valid_through text, state text,
            postal_code text, latitude numeric, longitude numeric, geo_da text,
            applicants_count integer, is_easy_apply boolean
        );
        CREATE TABLE tecnologie_ricerca (job_id uuid, tecnologia text, posted_at timestamptz);
        CREATE TABLE stipendi_benchmark (
            country text, currency text, family text, seniority text,
            p25 numeric, p50 numeric, p75 numeric, n integer, aggiornato timestamptz
        );
    """)
    rows = [
        {
            "id": "00000000-0000-0000-0000-00000000000a", "title": "Senior Python Engineer",
            "platform_id": "bundesanstellung", "slug": "acme", "country": "DE",
            "city": "Berlin", "lang": "en", "seniority": "senior", "remote": "hybrid",
            "skills": ["python", "ETL"], "salary_min": 60000, "salary_max": 90000,
            "posted_at": "2026-11-15T10:00:00+00:00", "company": "Acme Analytics",
            "domain": "acme.example", "source_type": "public_board",
            "family": "Data & Analytics", "technologies": ["python", "kubernetes"],
            "state": "Berlin", "employment_type": "full_time", "department": "Data",
            "description": "Python data pipelines with Kubernetes and reliable APIs",
        },
        {
            "id": "00000000-0000-0000-0000-00000000000b", "title": "Java Backend Developer",
            "platform_id": "greenhouse", "slug": "beta", "country": "FR",
            "city": "Paris", "lang": "fr", "seniority": "junior", "remote": "onsite",
            "skills": ["java"], "salary_min": 30000, "salary_max": 50000,
            "posted_at": "2026-11-10T10:00:00+00:00", "company": "Beta Industries",
            "domain": "beta.example", "source_type": "direct", "family": "Engineering",
            "technologies": ["java"], "state": "Ile-de-France",
            "employment_type": "part_time", "department": "Platform",
            "description": "Java services for reliable enterprise platforms",
        },
        {
            "id": "00000000-0000-0000-0000-00000000000c", "title": "Python Analyst",
            "platform_id": "bundesanstellung", "slug": "acme", "country": "DE",
            "city": "Munich", "lang": "en", "seniority": "mid", "remote": "remote",
            "skills": ["sql"], "salary_min": None, "salary_max": None,
            "posted_at": "2026-11-01T10:00:00+00:00", "company": "Acme Analytics",
            "domain": "acme.example", "source_type": "public_board",
            "family": "Data & Analytics", "technologies": ["sql"], "state": "Bavaria",
            "employment_type": "full_time", "department": "Data",
            "description": "SQL analysis for public sector labour market data",
        },
    ]
    companies = set()
    for row in rows:
        posted = row["posted_at"]
        connection.execute("""
            INSERT INTO ats_jobs (
                id, title, platform_id, slug, url, country, city, location, lang,
                seniority, remote, skills, salary_min, salary_max, salary_currency,
                posted_at, created_at, fetched_at, raw, employment_type,
                languages_required, external_id, department, salary_period, salary_da,
                posted_at_estimated, duplicate_key
            ) VALUES (
                %(id)s, %(title)s, %(platform_id)s, %(slug)s,
                'https://jobs.example/' || %(id_text)s,
                %(country)s, %(city)s, %(city)s, %(lang)s, %(seniority)s, %(remote)s,
                %(skills)s, %(salary_min)s, %(salary_max)s, 'EUR', %(posted_at)s,
                %(posted_at)s, %(posted_at)s, %(raw)s, %(employment_type)s,
                ARRAY[%(lang)s], %(id_text)s, %(department)s, 'year', 'declared',
                false, %(id_text)s
            )
        """, {**row, "id_text": row["id"], "raw": json.dumps({
            "company": {"name": row["company"]}, "description": row["description"],
        })})
        company_key = (row["platform_id"], row["slug"])
        if company_key not in companies:
            connection.execute(
                "INSERT INTO ats_companies VALUES (%s, %s, %s, %s, %s)",
                (row["platform_id"], row["slug"], row["company"],
                 row["source_type"], row["domain"]),
            )
            companies.add(company_key)
        connection.execute("INSERT INTO job_classifications VALUES (%s, %s)",
                           (row["id"], row["family"]))
        connection.execute("INSERT INTO tecnologie_v1 VALUES (%s, %s)",
                           (row["id"], row["technologies"]))
        connection.execute("INSERT INTO offerte_dettagli (job_id, state) VALUES (%s, %s)",
                           (row["id"], row["state"]))
        for technology in row["technologies"]:
            connection.execute("INSERT INTO tecnologie_ricerca VALUES (%s, %s, %s)",
                               (row["id"], technology, posted))
    monkeypatch.setattr(vivo, "_pool", None)
    monkeypatch.setattr(vivo, "_con", connection)
    monkeypatch.setattr(vivo, "_bench", {})
    monkeypatch.setattr(vivo, "_bench_controllo", float("inf"))
    yield {"a": rows[0]["id"], "b": rows[1]["id"], "c": rows[2]["id"]}
    connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
    connection.close()


def _ids(rows):
    return [row["id"] for row in rows]


@pytest.mark.parametrize(("filters", "expected"), [
    ({"country": "de"}, ["a", "c"]),
    ({"category": "Data & Analytics"}, ["a", "c"]),
    ({"ats": "greenhouse,Bundesagentur"}, ["a", "b", "c"]),
    ({"seniority": "senior"}, ["a"]),
    ({"remote": "hybrid,onsite"}, ["a", "b"]),
    ({"language": "en"}, ["a", "c"]),
    ({"q": "Python Kubernetes"}, ["a"]),
    ({"q_any": "Kubernetes,Java"}, ["a", "b"]),
    ({"title": "Python Engineer"}, ["a"]),
    ({"technology": "python,kubernetes"}, ["a"]),
    ({"source_type": "public_board"}, ["a", "c"]),
    ({"company": "Acme Analytics"}, ["a", "c"]),
    ({"company_slug": "acme,beta"}, ["a", "b", "c"]),
    ({"company_domain": "https://www.acme.example/path"}, ["a", "c"]),
    ({"city": "Berlin,Paris"}, ["a", "b"]),
    ({"state": "Berlin,Bavaria"}, ["a", "c"]),
    ({"employment_type": "full_time"}, ["a", "c"]),
    ({"department": "Data"}, ["a", "c"]),
    ({"skills": "python,java"}, ["a", "b"]),
    ({"salary_currency": "eur"}, ["a", "b", "c"]),
    ({"has_salary": True}, ["a", "b"]),
    ({"has_salary": False}, ["c"]),
    ({"salary_min": 50000}, ["a"]),
    ({"salary_max": 80000}, ["b"]),
    ({"dal": "2026-11-12T00:00:00Z"}, ["a"]),
    ({"posted_before": "2026-11-12T00:00:00Z"}, ["b", "c"]),
    ({"exclude_ats": "BUNDESAGENTUR"}, ["b"]),
    ({"ats": "greenhouse,Bundesagentur", "exclude_ats": "bundesagentur"}, ["b"]),
    ({"q": "Python", "source_type": "public_board", "skills": "python"}, ["a"]),
])
def test_postgres_fallback_matches_every_opensearch_job_filter(pg_vivo, filters, expected):
    rows, cursor = vivo.offerte(filters, None, 10)
    assert _ids(rows) == [pg_vivo[key] for key in expected]
    assert cursor is None


def test_postgres_fallback_parameters_prevent_sql_injection(pg_vivo):
    malicious = "acme' OR 1=1 --"
    rows, _ = vivo.offerte({"company_slug": malicious}, None, 10)
    assert rows == []
    rows, _ = vivo.offerte({}, None, 10)
    assert len(rows) == 3


def test_postgres_fallback_keyset_pagination_has_no_gaps_or_duplicates(pg_vivo):
    first, cursor = vivo.offerte({}, None, 2)
    assert _ids(first) == [pg_vivo["a"], pg_vivo["b"]]
    assert cursor
    second, cursor = vivo.offerte({}, cursor, 2)
    assert _ids(second) == [pg_vivo["c"]]
    assert cursor is None


def test_postgres_q_any_limit_matches_opensearch(pg_vivo):
    with pytest.raises(ValueError, match="at most 15"):
        vivo.offerte({"q_any": ",".join(f"term{i}" for i in range(16))}, None, 10)
