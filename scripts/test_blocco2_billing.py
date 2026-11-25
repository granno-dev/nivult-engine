import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from nivult.api import app as app_module
from nivult.api_clienti import billing

UID = "6c2c38de-7bb4-4f1e-b72b-92df9a0d06f1"


class FakeCursor:
    def __init__(self, state):
        self.state = state
        self.next = None

    def execute(self, sql, params=()):
        sql = " ".join(sql.split())
        self.params = params
        self.next = None
        if sql.startswith("INSERT INTO creem_eventi"):
            event_id = params[0]
            if event_id in self.state["events"]:
                return
            self.state["events"].add(event_id)
            self.next = (event_id,)
        elif sql.startswith("SELECT crediti_extra, status FROM users"):
            self.next = (self.state["extra"], "active")
        elif sql.startswith("SELECT id::text FROM users WHERE creem_customer_id"):
            self.next = (UID,)
        elif sql.startswith("SELECT creem_period_start FROM users"):
            self.next = (self.state.get("period_start"),)
        elif sql.startswith("INSERT INTO creem_subscription_eventi"):
            key = (params[0], params[1])
            if key in self.state["states"]:
                return
            self.state["states"].add(key)
            self.next = (params[2],)
        elif sql.startswith("UPDATE users SET subscription_status = 'active'"):
            self.state.update(plan=params[0], interval=params[1], period_end=params[2],
                              customer_id=params[3], subscription_id=params[4],
                              period_start=params[5], subscription_status="active")
        elif sql.startswith("UPDATE users SET subscription_status = 'scheduled_cancel'"):
            self.state.update(period_end=params[0], customer_id=params[1],
                              subscription_id=params[2], subscription_status="scheduled_cancel")
        elif sql.startswith("UPDATE users SET subscription_status = %s"):
            self.state.update(subscription_status=params[0], period_end=params[1],
                              customer_id=params[2], subscription_id=params[3])
        elif sql.startswith("UPDATE api_chiavi SET crediti_mensili = %s, usati_mese = 0"):
            self.state.update(quota=params[0], used=0)
        elif sql.startswith("UPDATE api_chiavi SET crediti_mensili = %s WHERE"):
            self.state["quota"] = params[0]
        elif sql.startswith("UPDATE api_chiavi SET crediti_mensili = 0"):
            self.state["quota"] = 0
        elif sql.startswith("UPDATE users SET crediti_extra = crediti_extra + %s"):
            self.state["extra"] += params[0]
            self.next = (self.state["extra"],)
        elif sql.startswith("INSERT INTO portale_acquisti"):
            self.next = (1,)

    def fetchone(self):
        value, self.next = self.next, None
        return value

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class FakeConnection:
    def __init__(self, state):
        self.cursor_instance = FakeCursor(state)

    def cursor(self):
        return self.cursor_instance

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _state():
    return {"events": set(), "states": set(), "extra": 77, "quota": 500,
            "used": 40, "period_start": None}


def _subscription_event(event_type, event_id, *, product="prod-growth-year",
                        transaction=None, start="2026-01-01T00:00:00Z"):
    last_transaction = {"id": transaction, "customer": None} if transaction else None
    return {
        "id": event_id, "eventType": event_type,
        "object": {
            "id": "sub-1", "customer": {"id": "cust-1"},
            "metadata": {"user_id": UID}, "product": {"id": product},
            "current_period_start_date": start,
            "current_period_end_date": "2027-01-01T00:00:00Z",
            "last_transaction_id": transaction,
            "last_transaction": last_transaction,
        },
    }


def _apply(monkeypatch, state, event):
    monkeypatch.setattr(billing.psycopg, "connect", lambda dsn: FakeConnection(state))
    return billing.applica_pagamento("postgresql://test", event)


def test_annual_activation_paid_and_manual_resend_do_not_double_reset(monkeypatch):
    monkeypatch.setenv("CREEM_PRODOTTI_ABBONAMENTI", json.dumps({
        "growth:year": "prod-growth-year", "scale:year": "prod-scale-year"}))
    state = _state()
    assert _apply(monkeypatch, state, _subscription_event("subscription.active", "evt-1")) \
        == "subscription attiva"
    assert state["quota"] == 100_000 and state["used"] == 0
    assert state["plan"] == "growth" and state["interval"] == "year"
    assert state["customer_id"] == "cust-1"

    state["used"] = 25
    paid = _subscription_event("subscription.paid", "evt-2", transaction="tx-1")
    _apply(monkeypatch, state, paid)
    assert state["used"] == 25

    state["used"] = 35
    resend = _subscription_event("subscription.paid", "evt-manual", transaction="tx-1")
    _apply(monkeypatch, state, resend)
    _apply(monkeypatch, state, resend)
    assert state["used"] == 35


def test_update_changes_plan_without_resetting_current_period(monkeypatch):
    monkeypatch.setenv("CREEM_PRODOTTI_ABBONAMENTI", json.dumps({
        "growth:year": "prod-growth-year", "scale:year": "prod-scale-year"}))
    state = _state()
    _apply(monkeypatch, state, _subscription_event("subscription.active", "evt-1"))
    state["used"] = 42
    update = _subscription_event("subscription.update", "evt-2", product="prod-scale-year")
    _apply(monkeypatch, state, update)
    assert state["plan"] == "scale" and state["quota"] == 500_000
    assert state["used"] == 42


def test_scheduled_cancel_keeps_access_and_cancel_preserves_topups(monkeypatch):
    monkeypatch.setenv("CREEM_PRODOTTI_ABBONAMENTI", json.dumps({
        "growth:year": "prod-growth-year"}))
    state = _state()
    _apply(monkeypatch, state, _subscription_event("subscription.active", "evt-1"))
    extra = state["extra"]
    state["used"] = 21
    _apply(monkeypatch, state, _subscription_event("subscription.scheduled_cancel", "evt-2"))
    assert state["subscription_status"] == "scheduled_cancel"
    assert state["quota"] == 100_000 and state["extra"] == extra
    _apply(monkeypatch, state, _subscription_event("subscription.canceled", "evt-3"))
    assert state["subscription_status"] == "canceled"
    assert state["quota"] == 0 and state["extra"] == extra


@pytest.mark.parametrize("event_type,status", [
    ("subscription.expired", "expired"),
    ("subscription.paused", "paused"),
    ("subscription.unpaid", "unpaid"),
])
def test_terminal_events_revoke_only_monthly_quota(monkeypatch, event_type, status):
    monkeypatch.setenv("CREEM_PRODOTTI_ABBONAMENTI", json.dumps({
        "growth:year": "prod-growth-year"}))
    state = _state()
    _apply(monkeypatch, state, _subscription_event("subscription.active", "evt-1"))
    extra = state["extra"]
    _apply(monkeypatch, state, _subscription_event(event_type, "evt-terminal"))
    assert state["subscription_status"] == status
    assert state["quota"] == 0 and state["extra"] == extra


def test_one_time_topup_and_subscription_checkout_are_separate(monkeypatch):
    state = _state()
    event = {
        "id": "checkout-1", "eventType": "checkout.completed",
        "object": {"id": "checkout-1", "metadata": {
            "kind": "topup", "user_id": UID, "volume": 10_000}},
    }
    assert _apply(monkeypatch, state, event) == "accreditato (1 utente)"
    assert state["extra"] == 10_077 and state["quota"] == 500
    subscription_checkout = {
        "id": "checkout-2", "eventType": "checkout.completed",
        "object": {"metadata": {"kind": "subscription", "user_id": UID}},
    }
    assert _apply(monkeypatch, state, subscription_checkout) == "ignorato-abbonamento"
    assert state["extra"] == 10_077


def test_topup_linear_price_and_minimum():
    assert billing.euro_per_crediti(10_000) == 40
    assert billing.euro_per_crediti(25_000) == 100
    with pytest.raises(billing.HTTPException) as exc:
        billing.checkout(UID, "user@example.com", 1_000, "https://ok", "https://cancel")
    assert exc.value.status_code == 400


def test_hmac_signature(monkeypatch):
    monkeypatch.setenv("CREEM_WEBHOOK_SECRET", "test-secret")
    body = b'{"id":"evt"}'
    signature = hmac.new(b"test-secret", body, hashlib.sha256).hexdigest()
    assert billing.firma_valida(body, signature)
    assert not billing.firma_valida(body, "invalid")


class CreemResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload
        self.text = ""

    def json(self):
        return self.payload


def test_checkout_upgrade_and_customer_portal_mocked(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs["json"]))
        payload = ({"customer_portal_link": "https://creem.test/portal"}
                   if url.endswith("/customers/billing") else {"checkout_url": "https://creem.test/checkout"})
        return CreemResponse(payload)

    monkeypatch.setattr(billing.httpx, "request", request)
    monkeypatch.setenv("CREEM_API_KEY", "test-key")
    monkeypatch.setenv("CREEM_PRODOTTI_ABBONAMENTI", json.dumps({
        "growth:month": "prod-growth-month"}))
    url = billing.checkout_abbonamento(UID, "user@example.com", "growth", "month",
                                      "https://nivult.com/account", "https://nivult.com/account")
    assert url == "https://creem.test/checkout"
    checkout = calls[0][2]
    assert set(checkout) <= {
        "product_id", "request_id", "units", "custom_price", "trial_period_days",
        "discount_code", "affiliate_code", "customer", "custom_fields",
        "success_url", "metadata"}
    assert "cancel_url" not in checkout and "custom_price" not in checkout
    assert set(checkout["customer"]) <= {"id", "email", "name"}
    assert checkout["success_url"] == "https://nivult.com/account"
    assert ".html" not in checkout["success_url"]
    assert checkout["product_id"] == "prod-growth-month"
    billing.upgrade_abbonamento("sub-1", "growth", "month")
    assert calls[1][0] == "POST" and calls[1][1].endswith("/subscriptions/sub-1/upgrade")
    assert set(calls[1][2]) <= {"product_id", "update_behavior"}
    assert calls[1][2]["update_behavior"] == "proration-charge-immediately"
    assert billing.customer_portal("cust-1") == "https://creem.test/portal"
    assert calls[2][2] == {"customer_id": "cust-1"}
    assert set(calls[2][2]) <= {"customer_id"}


def test_topup_checkout_payload_whitelist(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return CreemResponse({"checkout_url": "https://creem.test/topup"})

    monkeypatch.setattr(billing.httpx, "post", post)
    monkeypatch.setenv("CREEM_API_KEY", "test-key")
    monkeypatch.setenv("CREEM_PRODOTTO", "prod-topup")
    url = billing.checkout(UID, "user@example.com", 10_000,
                           "https://nivult.com/account?pagato=1",
                           "https://nivult.com/account")
    assert url == "https://creem.test/topup"
    payload = calls[0]
    assert set(payload) <= {
        "product_id", "request_id", "units", "custom_price", "trial_period_days",
        "discount_code", "affiliate_code", "customer", "custom_fields",
        "success_url", "metadata"}
    assert "cancel_url" not in payload
    assert payload["custom_price"] == 4000 and payload["units"] == 1
    assert set(payload["customer"]) <= {"id", "email", "name"}
    assert payload["success_url"].startswith("https://nivult.com/account")
    assert ".html" not in payload["success_url"]


def test_existing_subscription_uses_upgrade_not_second_checkout(monkeypatch):
    application = app_module.create_app()
    route = next(route for route in application.routes if route.path == "/me/checkout")

    class Cursor:
        def execute(self, sql, params):
            pass

        def fetchone(self):
            return ("user@example.com", "sub-1", "active", "starter", "month")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    class Connection:
        def cursor(self):
            return Cursor()

    for dependency in route.dependant.dependencies:
        if dependency.name == "uid":
            application.dependency_overrides[dependency.call] = lambda: UID
        elif dependency.name == "conn":
            application.dependency_overrides[dependency.call] = lambda: Connection()

    calls = []
    monkeypatch.setattr(billing, "upgrade_abbonamento",
                        lambda *args: calls.append(("upgrade", args)) or {})
    monkeypatch.setattr(billing, "checkout_abbonamento",
                        lambda *args: calls.append(("checkout", args)) or "https://invalid")
    response = TestClient(application).post("/me/checkout", json={
        "plan": "growth", "interval": "month"})
    assert response.status_code == 200
    assert response.json() == {"upgraded": True}
    assert [call[0] for call in calls] == ["upgrade"]
