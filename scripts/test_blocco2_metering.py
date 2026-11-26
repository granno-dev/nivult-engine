from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nivult.api import app as app_module
from nivult.api_clienti import chiavi, dati, opensearch, v1


def _client(saldo=500, quota=500, status="trialing", subscription_id=None):
    cliente = {
        "id": "key-test", "user_id": "user-test", "monthly_tier": quota,
        "crediti_residui": saldo, "subscription_status": status,
        "creem_subscription_id": subscription_id,
        "subscription_plan": "growth" if quota >= 100_000 else None,
        "subscription_interval": "year",
        "current_period_end": None,
    }
    app = FastAPI()
    app.include_router(v1.router)
    app.dependency_overrides[v1._cliente] = lambda: cliente
    return TestClient(app)


def _capture(monkeypatch, target, rows):
    calls = {"query": 0, "limits": [], "charges": []}

    def query(filtri, cursor, limit):
        calls["query"] += 1
        calls["limits"].append(limit)
        return rows[:limit], None

    def charge(*args, **kwargs):
        calls["charges"].append(kwargs["crediti"])
        return {}

    return calls, query, charge


def test_jobs_charge_one_credit_per_record(monkeypatch):
    calls, query, charge = _capture(monkeypatch, opensearch.offerte, [{}, {}, {}])
    monkeypatch.setattr(opensearch, "attivo", lambda: True)
    monkeypatch.setattr(opensearch, "offerte", query)
    monkeypatch.setattr(chiavi, "addebita_chiave", charge)
    response = _client().get("/v1/jobs?limit=3")
    assert response.status_code == 200
    assert response.json()["count_page"] == 3
    assert calls["charges"] == [3]


def test_companies_charge_three_credits_per_record(monkeypatch):
    calls, query, charge = _capture(monkeypatch, dati.aziende, [{}, {}])
    monkeypatch.setattr(dati, "aziende", query)
    monkeypatch.setattr(chiavi, "addebita_chiave", charge)
    response = _client(quota=100_000).get("/v1/companies?limit=2")
    assert response.status_code == 200
    assert calls["charges"] == [6]


def test_empty_results_and_validation_errors_are_free(monkeypatch):
    calls, query, charge = _capture(monkeypatch, opensearch.offerte, [])
    monkeypatch.setattr(opensearch, "attivo", lambda: True)
    monkeypatch.setattr(opensearch, "offerte", query)
    monkeypatch.setattr(chiavi, "addebita_chiave", charge)
    client = _client()
    assert client.get("/v1/jobs").json()["count_page"] == 0
    assert client.get("/v1/jobs?salary_min=abc").status_code == 400
    assert calls["query"] == 1
    assert calls["charges"] == []


def test_balance_caps_page_and_insufficient_balance_skips_query(monkeypatch):
    calls, query, charge = _capture(monkeypatch, opensearch.offerte, [{}, {}, {}])
    monkeypatch.setattr(opensearch, "attivo", lambda: True)
    monkeypatch.setattr(opensearch, "offerte", query)
    monkeypatch.setattr(chiavi, "addebita_chiave", charge)
    response = _client(saldo=2).get("/v1/jobs?limit=500")
    assert response.status_code == 200
    assert calls["limits"] == [2]
    assert calls["charges"] == [2]

    calls, query, charge = _capture(monkeypatch, dati.aziende, [{}, {}])
    monkeypatch.setattr(dati, "aziende", query)
    monkeypatch.setattr(chiavi, "addebita_chiave", charge)
    response = _client(saldo=2).get("/v1/companies")
    assert response.status_code == 429
    assert calls["query"] == 0
    assert calls["charges"] == []


def test_changes_use_per_record_metering_and_active_growth_entitlement(monkeypatch):
    calls = {"query": 0, "charges": []}

    def query(since, cursor, limit):
        calls["query"] += 1
        return [{}, {}], None

    monkeypatch.setattr(dati, "cambiamenti", query)
    monkeypatch.setattr(chiavi, "addebita_chiave",
                        lambda *args, **kwargs: calls["charges"].append(kwargs["crediti"]))
    client = _client(saldo=1000, quota=100_000, status="active", subscription_id="sub")
    response = client.get("/v1/changes?since=2026-11-24T00:00:00Z")
    assert response.status_code == 200
    assert calls == {"query": 1, "charges": [2]}

    denied = _client(quota=100_000, status="canceled", subscription_id="sub")
    assert denied.get("/v1/changes?since=2026-11-24T00:00:00Z").status_code == 403
    assert calls["query"] == 1


def test_bulk_requires_active_scale_and_scheduled_cancel_respects_period_end():
    active = {"monthly_tier": 500_000, "subscription_status": "active",
              "creem_subscription_id": "sub"}
    v1._richiedi_volume(active, v1.SOGLIA_BULK, "bulk")
    with pytest.raises(Exception) as exc:
        v1._richiedi_volume({**active, "monthly_tier": 100_000},
                            v1.SOGLIA_BULK, "bulk")
    assert exc.value.status_code == 403

    future = datetime.now(UTC) + timedelta(days=2)
    scheduled = {**active, "subscription_status": "scheduled_cancel",
                 "current_period_end": future}
    v1._richiedi_volume(scheduled, v1.SOGLIA_BULK, "bulk")
    expired = {**scheduled, "current_period_end": future - timedelta(days=3)}
    with pytest.raises(Exception) as exc:
        v1._richiedi_volume(expired, v1.SOGLIA_BULK, "bulk")
    assert exc.value.status_code == 403


def test_rapidapi_cannot_access_direct_only_delta_or_exports():
    app = FastAPI()
    app.include_router(v1.router)
    app.dependency_overrides[v1._cliente] = lambda: {
        "id": "rapidapi", "user_id": None, "via": "rapidapi",
        "utente_rapidapi": "buyer", "monthly_tier": 10_000,
    }
    client = TestClient(app)
    for path in ("/v1/changes?since=2026-11-24T00:00:00Z",
                 "/v1/exports/latest", "/v1/exports/latest/offerte"):
        response = client.get(path)
        assert response.status_code == 403
        assert "RapidAPI" in response.json()["detail"] or "direct Nivult" in response.json()["detail"]


def test_annual_subscription_resets_monthly_but_trial_does_not():
    previous_month = datetime.now(UTC).date().replace(day=1) - timedelta(days=1)
    rows = [{"crediti_mensili": 500, "usati_mese": 100, "mese_uso": previous_month}]
    assert chiavi._reset_abbonamento("active", "sub-annual", None) is True
    annual = chiavi._riepilogo_da_righe(rows, 0, resetta_mensile=True)
    assert annual["usati"] == 0 and annual["saldo"] == 500
    trial = chiavi._riepilogo_da_righe(rows, 0, resetta_mensile=False)
    assert trial["usati"] == 100 and trial["saldo"] == 400
    assert chiavi._reset_abbonamento("trialing", None, None) is False


def _stub_service(monkeypatch, quota, used, extra=0):
    calls = {"service": 0, "charges": []}
    remaining = max(0, quota - used)

    def service(key_id):
        calls["service"] += 1
        return {"crediti_mensili": quota, "usati": used, "usati_mese": used,
                "usati_chiave": used, "crediti_extra": extra,
                "residui_mensili": remaining, "saldo": remaining + extra,
                "mese_uso": date.today().replace(day=1)}

    monkeypatch.setattr(chiavi, "registra_servizio_chiave", service)
    monkeypatch.setattr(chiavi, "addebita_chiave",
                        lambda *args, **kwargs: calls["charges"].append(kwargs))
    return calls


def test_usage_and_coverage_are_free_at_zero_balance(monkeypatch):
    calls = _stub_service(monkeypatch, 500, 500)
    monkeypatch.setattr(dati, "copertura", lambda: {"country": 100})
    client = _client(saldo=0)
    first = client.get("/v1/usage")
    second = client.get("/v1/usage")
    assert first.status_code == second.status_code == 200
    assert first.headers["X-Credits-Charged"] == "0"
    assert first.json()["balance"] == second.json()["balance"] == 0
    assert first.json()["used"] == second.json()["used"] == 500
    coverage = client.get("/v1/coverage")
    assert coverage.status_code == 200 and coverage.headers["X-Credits-Charged"] == "0"
    assert calls["service"] == 3 and calls["charges"] == []


@pytest.mark.parametrize("balance", [0, 250])
def test_scale_manifest_and_download_are_free_at_any_balance(monkeypatch, tmp_path, balance):
    path = tmp_path / "offerte.jsonl.gz"
    path.write_bytes(b"preview")
    monkeypatch.setattr(dati, "stato_export", lambda: {"file": {"offerte": str(path)}})
    monkeypatch.setattr(dati, "stream_export_senza_pii", lambda _: iter([b"preview"]))
    calls = _stub_service(monkeypatch, 500_000, 500_000 - balance)
    client = _client(saldo=balance, quota=500_000, status="active", subscription_id="sub")
    manifest = client.get("/v1/exports/latest")
    assert manifest.status_code == 200 and manifest.headers["X-Credits-Charged"] == "0"
    response = client.get("/v1/exports/latest/offerte")
    assert response.status_code == 200 and response.content == b"preview"
    assert response.headers["X-Credits-Charged"] == "0"
    assert calls["service"] == 2 and calls["charges"] == []


@pytest.mark.parametrize("quota,status,subscription_id,balance", [
    (500, "trialing", None, 0),
    (15_000, "active", "sub", 100),
    (100_000, "active", "sub", 100),
    (500, "trialing", None, 10_000),
])
def test_non_scale_bulk_is_forbidden_without_charges(
        monkeypatch, quota, status, subscription_id, balance):
    calls = _stub_service(monkeypatch, quota, quota)
    client = _client(saldo=balance, quota=quota, status=status,
                     subscription_id=subscription_id)
    assert client.get("/v1/exports/latest").status_code == 403
    assert client.get("/v1/exports/latest/offerte").status_code == 403
    assert calls["service"] == 0 and calls["charges"] == []


def test_revoked_key_is_rejected_on_service_endpoints(monkeypatch):
    def revoked(_):
        raise chiavi.ChiaveInvalida()

    monkeypatch.setattr(chiavi, "verifica", revoked)
    app = FastAPI()
    app.include_router(v1.router)
    client = TestClient(app)
    for path in ("/v1/usage", "/v1/coverage", "/v1/exports/latest",
                 "/v1/exports/latest/offerte"):
        assert client.get(path, headers={"X-Api-Key": "revoked"}).status_code == 401


def test_service_endpoints_keep_rate_limit_at_zero_balance(monkeypatch, tmp_path):
    path = tmp_path / "offerte.jsonl.gz"
    path.write_bytes(b"preview")
    monkeypatch.setattr(dati, "copertura", lambda: {"country": 100})
    monkeypatch.setattr(dati, "stato_export", lambda: {"file": {"offerte": str(path)}})
    monkeypatch.setattr(dati, "stream_export_senza_pii", lambda _: iter([b"preview"]))
    calls = _stub_service(monkeypatch, 500_000, 500_000)
    monkeypatch.setattr(chiavi, "saldo_per_chiave", lambda _: 0)
    monkeypatch.setattr(chiavi, "verifica", lambda _: {
        "id": "scale-key", "user_id": "scale-user", "monthly_tier": 500_000,
        "subscription_status": "active", "creem_subscription_id": "sub",
        "current_period_end": None,
    })

    class OneRequestLimiter:
        def __init__(self):
            self.calls = 0

        def allow(self, identity, rate, burst):
            self.calls += 1
            return (True, 0) if self.calls == 1 else (False, 1)

    limiter = OneRequestLimiter()
    monkeypatch.setattr(v1, "_rate_limiter", limiter)
    app = FastAPI()
    app.include_router(v1.router)
    client = TestClient(app)
    for path_url in ("/v1/usage", "/v1/coverage", "/v1/exports/latest",
                     "/v1/exports/latest/offerte"):
        limiter.calls = 0
        first = client.get(path_url, headers={"X-Api-Key": "scale"})
        second = client.get(path_url, headers={"X-Api-Key": "scale"})
        assert first.status_code == 200
        assert second.status_code == 429 and second.headers["Retry-After"] == "1"
    assert calls["charges"] == []


@pytest.mark.parametrize("path_url", [
    "/v1/jobs", "/v1/companies", "/v1/changes?since=2026-11-24T00:00:00Z",
])
def test_paid_endpoints_still_reject_zero_balance(monkeypatch, path_url):
    calls = _stub_service(monkeypatch, 100_000, 100_000)
    client = _client(saldo=0, quota=100_000, status="active", subscription_id="sub")
    assert client.get(path_url).status_code == 429
    assert calls["service"] == 0 and calls["charges"] == []


def _portal_export_client():
    application = app_module.create_app()
    route = next(route for route in application.routes
                 if route.path == "/portale/export/{nome}")
    for dependency in route.dependant.dependencies:
        if dependency.name == "uid":
            application.dependency_overrides[dependency.call] = lambda: "portal-user"
    return TestClient(application)


def _stub_portal_export(monkeypatch, tmp_path, active=True):
    path = tmp_path / "portal-export.jsonl.gz"
    path.write_bytes(b"preview")
    monkeypatch.setattr(chiavi, "lista_per_utente",
                        lambda _: [{"id": "portal-key", "revocata_il": None}])
    monkeypatch.setattr(chiavi, "abbonamento_per_utente", lambda _: {
        "subscription_status": "active" if active else "trialing",
        "creem_subscription_id": "sub" if active else None,
        "current_period_end": None,
    })
    monkeypatch.setattr(chiavi, "volume_per_utente", lambda _: 500_000)
    monkeypatch.setattr(chiavi, "consuma_quota_explorer", lambda *_: None)
    monkeypatch.setattr(dati, "stato_export", lambda: {"file": {"offerte": str(path)}})
    monkeypatch.setattr(dati, "stream_export_senza_pii", lambda _: iter([b"preview"]))
    calls = _stub_service(monkeypatch, 500_000, 500_000)
    monkeypatch.setattr(chiavi, "spendi_n_per_utente",
                        lambda *args, **kwargs: calls["charges"].append(kwargs))
    return calls


def test_portal_daily_bulk_is_free_at_zero_and_rate_limited(monkeypatch, tmp_path):
    calls = _stub_portal_export(monkeypatch, tmp_path)
    hits = 0

    def quota(*_):
        nonlocal hits
        hits += 1
        if hits > 20:
            raise chiavi.QuotaExplorer(60, "user_minute")

    monkeypatch.setattr(chiavi, "consuma_quota_explorer", quota)
    client = _portal_export_client()
    responses = [client.get("/portale/export/offerte") for _ in range(21)]
    assert [r.status_code for r in responses] == [200] * 20 + [429]
    assert responses[-1].headers["Retry-After"] == "60"
    assert calls["service"] == 20 and calls["charges"] == []


def test_portal_daily_bulk_still_requires_active_scale(monkeypatch, tmp_path):
    calls = _stub_portal_export(monkeypatch, tmp_path, active=False)
    client = _portal_export_client()
    assert client.get("/portale/export/offerte").status_code == 403
    assert calls["service"] == 0 and calls["charges"] == []


RAPID_HEADERS = {"X-RapidAPI-Proxy-Secret": "test-secret",
                 "X-RapidAPI-User": "buyer"}


def _rapid_billing_client(monkeypatch):
    monkeypatch.setenv("RAPIDAPI_PROXY_SECRET", "test-secret")
    monkeypatch.setattr(v1, "_applica_rate_limit", lambda *args: None)
    app = FastAPI()
    app.middleware("http")(v1.billing_rapidapi)
    app.include_router(v1.router)
    return TestClient(app)


def test_rapidapi_jobs_billing_empty_validation_and_direct_only(monkeypatch):
    rows = [{"id": "1"}, {"id": "2"}]
    monkeypatch.setattr(opensearch, "attivo", lambda: True)
    monkeypatch.setattr(opensearch, "offerte",
                        lambda filters, cursor, limit: (rows[:limit], None))
    client = _rapid_billing_client(monkeypatch)
    response = client.get("/v1/jobs?limit=2", headers=RAPID_HEADERS)
    assert response.status_code == 200
    assert response.headers["X-RapidAPI-Billing"] == "Credits=2"

    rows.clear()
    response = client.get("/v1/jobs", headers=RAPID_HEADERS)
    assert response.status_code == 200
    assert response.headers["X-RapidAPI-Billing"] == "Credits=0"

    for path in ("/v1/jobs?salary_min=invalid", "/v1/jobs?limit=0",
                 "/v1/changes?since=2026-11-24T00:00:00Z",
                 "/v1/exports/latest"):
        response = client.get(path, headers=RAPID_HEADERS)
        assert response.status_code in (400, 403, 422)
        assert response.headers["X-RapidAPI-Billing"] == "Credits=0"


def test_rapidapi_companies_and_detail_billing(monkeypatch):
    rows = [{"company": "A"}, {"company": "B"}]
    monkeypatch.setattr(dati, "aziende",
                        lambda filters, cursor, limit: (rows[:limit], None))
    monkeypatch.setattr(dati, "azienda",
                        lambda platform, slug: {"company": "A"} if slug == "acme" else None)
    client = _rapid_billing_client(monkeypatch)
    response = client.get("/v1/companies?limit=2", headers=RAPID_HEADERS)
    assert response.status_code == 200
    assert response.headers["X-RapidAPI-Billing"] == "Credits=6"

    rows.clear()
    response = client.get("/v1/companies", headers=RAPID_HEADERS)
    assert response.headers["X-RapidAPI-Billing"] == "Credits=0"

    response = client.get("/v1/companies/greenhouse/acme", headers=RAPID_HEADERS)
    assert response.status_code == 200
    assert response.headers["X-RapidAPI-Billing"] == "Credits=3"
    response = client.get("/v1/companies/greenhouse/missing", headers=RAPID_HEADERS)
    assert response.status_code == 404
    assert response.headers["X-RapidAPI-Billing"] == "Credits=0"


def test_rapidapi_coverage_and_usage_bill_zero(monkeypatch):
    monkeypatch.setattr(dati, "copertura", lambda: {"country": 100})
    client = _rapid_billing_client(monkeypatch)
    for path in ("/v1/coverage", "/v1/usage"):
        response = client.get(path, headers=RAPID_HEADERS)
        assert response.status_code == 200
        assert response.headers["X-RapidAPI-Billing"] == "Credits=0"
        assert "X-Credits-Charged" not in response.headers


def test_rapidapi_rate_limit_and_server_errors(monkeypatch):
    client = _rapid_billing_client(monkeypatch)

    def limited(*args):
        raise v1.HTTPException(429, "limited")

    monkeypatch.setattr(v1, "_applica_rate_limit", limited)
    response = client.get("/v1/jobs", headers=RAPID_HEADERS)
    assert response.status_code == 429
    assert response.headers["X-RapidAPI-Billing"] == "Credits=0"

    monkeypatch.setattr(v1, "_applica_rate_limit", lambda *args: None)
    monkeypatch.setattr(opensearch, "attivo", lambda: True)

    def broken(*args):
        raise v1.psycopg.OperationalError("down")

    monkeypatch.setattr(opensearch, "offerte", broken)
    response = client.get("/v1/jobs", headers=RAPID_HEADERS)
    assert response.status_code == 503
    assert "X-RapidAPI-Billing" not in response.headers


def test_direct_api_never_gets_rapidapi_billing_header(monkeypatch):
    monkeypatch.setattr(opensearch, "attivo", lambda: True)
    monkeypatch.setattr(opensearch, "offerte",
                        lambda filters, cursor, limit: ([{"id": "1"}], None))
    monkeypatch.setattr(chiavi, "addebita_chiave", lambda *args, **kwargs: {})
    app = FastAPI()
    app.middleware("http")(v1.billing_rapidapi)
    app.include_router(v1.router)
    app.dependency_overrides[v1._cliente] = lambda: {
        "id": "direct-key", "user_id": "direct-user", "monthly_tier": 100_000,
        "crediti_residui": 100, "subscription_status": "active",
        "creem_subscription_id": "sub", "current_period_end": None,
    }
    response = TestClient(app).get("/v1/jobs", headers={"X-Api-Key": "direct"})
    assert response.status_code == 200
    assert "X-RapidAPI-Billing" not in response.headers


def test_production_app_registers_rapidapi_billing_middleware():
    app = app_module.create_app()
    assert any(middleware.kwargs.get("dispatch") is v1.billing_rapidapi
               for middleware in app.user_middleware)
