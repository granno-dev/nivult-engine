from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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


def test_annual_subscription_resets_monthly_but_trial_does_not():
    previous_month = datetime.now(UTC).date().replace(day=1) - timedelta(days=1)
    rows = [{"crediti_mensili": 500, "usati_mese": 100, "mese_uso": previous_month}]
    assert chiavi._reset_abbonamento("active", "sub-annual", None) is True
    annual = chiavi._riepilogo_da_righe(rows, 0, resetta_mensile=True)
    assert annual["usati"] == 0 and annual["saldo"] == 500
    trial = chiavi._riepilogo_da_righe(rows, 0, resetta_mensile=False)
    assert trial["usati"] == 100 and trial["saldo"] == 400
    assert chiavi._reset_abbonamento("trialing", None, None) is False
