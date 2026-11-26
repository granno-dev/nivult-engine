from __future__ import annotations

import os
import re
import secrets
import uuid
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from fastapi.testclient import TestClient

from nivult import auth
from nivult.api import app as app_module

ROOT = Path(__file__).resolve().parents[1]


def test_account_logout_frontend_clears_views_state_and_bfcache():
    html = (ROOT / "vetrina" / "account.html").read_text()
    assert "[hidden] { display: none !important; }" in html
    assert re.search(r'id="vista-dash"\s+hidden\b', html)
    assert re.search(r'id="vista-login"\s+hidden\b', html)
    assert 'el.style.removeProperty("display")' in html
    assert "el.hidden = !visibile" in html
    assert "pulisciStorageAccount(sessionStorage)" in html
    assert "pulisciStorageAccount(localStorage)" in html
    assert 'document.getElementById("vista-dash").innerHTML = ""' in html
    assert "chiaveFresca = \"\"" in html
    assert "chiavi = []" in html
    assert "revealInfo, revealRows, gruppiRivelati" in html
    assert 'await api("/auth/logout", { method: "POST", credentials: "include" })' in html
    assert "window.location.replace('/account')" in html
    assert re.search(r"addEventListener\('pageshow'.*if \(e\.persisted\).*reload", html,
                     re.DOTALL)
    init = html.index("await verificaGettone();")
    load = html.index("await carica();", init)
    show = html.index("mostraDashboard();", load)
    assert load < show


def test_logout_cors_preflight_allows_credentials_on_allowlisted_origin(monkeypatch):
    monkeypatch.setenv("B2B_ENABLED", "1")
    monkeypatch.setenv("CORS_ORIGINS", "https://portal.test")
    client = TestClient(app_module.create_app())
    request_headers = {
        "Origin": "https://portal.test",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }
    allowed = client.options("/auth/logout", headers=request_headers)
    assert allowed.status_code == 200
    assert allowed.headers["Access-Control-Allow-Origin"] == "https://portal.test"
    assert allowed.headers["Access-Control-Allow-Credentials"] == "true"
    rejected = client.options("/auth/logout", headers={
        **request_headers, "Origin": "https://attacker.example",
    })
    assert rejected.status_code == 400
    assert "Access-Control-Allow-Origin" not in rejected.headers
    client.close()


def _local_test_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL is required for the real session revocation test")
    host = (urlsplit(dsn).hostname if dsn.startswith(("postgresql://", "postgres://"))
            else psycopg.conninfo.conninfo_to_dict(dsn).get("host", ""))
    assert host in (None, "", "localhost", "127.0.0.1", "::1") or host.startswith("/")
    return dsn


def test_logout_revokes_bearer_and_replay_fails_on_me_and_explorer(monkeypatch):
    dsn = _local_test_dsn()
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("B2B_ENABLED", "1")
    monkeypatch.setenv("CORS_ORIGINS", "https://portal.test")
    monkeypatch.setenv("EXPLORER_REVEAL_SECRET", "logout-test-secret-32-bytes-minimum")
    uid = str(uuid.uuid4())
    token = secrets.token_urlsafe(32)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with psycopg.connect(dsn) as connection:
            connection.execute(
                "INSERT INTO users (id, email, plan, subscription_status, frequency) "
                "VALUES (%s, %s, 'basic', 'trialing', 'daily')",
                (uid, f"logout-{uid}@example.test"),
            )
            connection.execute(
                "INSERT INTO sessions (user_id, token_hash, expires_at, origin) "
                "VALUES (%s, %s, now() + interval '1 day', 'magic_link')",
                (uid, auth._sha256(token)),
            )

        client = TestClient(app_module.create_app())
        valid = client.get("/me", headers=headers)
        assert valid.status_code == 200
        assert valid.headers["Cache-Control"] == "no-store"

        logout = client.post("/auth/logout", headers=headers)
        assert logout.status_code == 200
        assert logout.headers["Cache-Control"] == "no-store"
        cookie = SimpleCookie()
        cookie.load(logout.headers["Set-Cookie"])
        expired = cookie["nv_sessione"]
        assert expired.value == ""
        assert expired["max-age"] == "0"
        assert expired["expires"]
        assert expired["path"] == "/"
        assert expired["secure"]
        assert expired["httponly"]
        assert expired["samesite"].lower() == "lax"

        with psycopg.connect(dsn) as connection:
            revoked = connection.execute(
                "SELECT revoked_at IS NOT NULL FROM sessions WHERE token_hash = %s",
                (auth._sha256(token),),
            ).fetchone()[0]
        assert revoked is True

        replay_me = client.get("/me", headers=headers)
        replay_explorer = client.post("/portale/cerca", headers=headers, json={})
        assert replay_me.status_code == 401
        assert replay_explorer.status_code == 401
        assert replay_me.headers["Cache-Control"] == "no-store"
        assert replay_explorer.headers["Cache-Control"] == "no-store"
        client.close()
    finally:
        with psycopg.connect(dsn) as connection:
            connection.execute("DELETE FROM users WHERE id = %s", (uid,))
