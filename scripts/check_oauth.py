#!/usr/bin/env python3
"""Verifica dell'accesso OAuth web e nativo.

    python scripts/check_oauth.py

Non parla con i provider: il client HTTP è iniettato e l'id_token è costruito
qui. Si provano callback Nivult/Mavult, state, claim e scadenza OIDC, PKCE,
pre-hijack Microsoft, collegamento autenticato, revoche GDPR e credenziali legacy.

DISTRUTTIVO: scrive utenti e poi ripulisce. Solo su database _test/_dev.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from nivult import auth, gdpr, oauth  # noqa: E402
from nivult.api.app import create_app  # noqa: E402
from nivult.config import database_name, database_url, safe_dsn  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []

GOOGLE_ID = "test-google-client-id"
MS_ID = "test-microsoft-client-id"
MSA = oauth.MSA_TENANT
TENANT_AZIENDALE = "11111111-2222-3333-4444-555555555555"


def check(label: str, got, expected) -> None:
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}  ->  {got!r}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} — atteso {expected!r}, ottenuto {got!r}")


def _b64(d: dict) -> str:
    grezzo = json.dumps(d).encode()
    return base64.urlsafe_b64encode(grezzo).decode().rstrip("=")


def id_token(claim: dict) -> str:
    """Un id_token con firma finta: non la verifichiamo, per scelta motivata."""
    return f"{_b64({'alg': 'RS256'})}.{_b64(claim)}.firma-non-verificata"


class Risposta:
    def __init__(self, status: int, corpo: dict):
        self.status_code = status
        self._corpo = corpo
        self.text = json.dumps(corpo)

    def json(self):
        return self._corpo


class ClientFinto:
    """Sta al posto di httpx: risponde con l'id_token che gli si dà."""

    def __init__(self, claim: dict | None = None, status: int = 200):
        self.claim = claim
        self.status = status

    def post(self, url, data=None, headers=None):
        if self.claim is None:
            return Risposta(self.status, {"errore": "no"})
        return Risposta(self.status, {"id_token": id_token(self.claim)})


def sfida_nativa(verifier: str) -> str:
    return base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def avvia(conn, provider: str, *, link_user_id: str | None = None) -> tuple[str, str]:
    """Apre un giro e ne estrae state e nonce dalla URL di autorizzazione."""
    url = oauth.inizia(conn, provider, link_user_id=link_user_id)
    q = parse_qs(urlparse(url).query)
    return q["state"][0], q["nonce"][0]


def esito(conn, provider, code, state, claim, status=200) -> str | None:
    """concludi(), ma il codice d'errore al posto dell'eccezione."""
    try:
        return oauth.concludi(conn, provider, code, state,
                              client=ClientFinto(claim, status))[0]
    except oauth.OAuthError as e:
        return f"errore:{e.codice}"


def claim_google(sub, email, verificata=True, nonce="", aud=None):
    return {"iss": "https://accounts.google.com", "aud": aud or GOOGLE_ID,
            "sub": sub, "email": email, "email_verified": verificata,
            "nonce": nonce, "exp": time.time() + 300}


def claim_microsoft(sub, email, tid=MSA, nonce="", aud=None):
    return {"iss": f"https://login.microsoftonline.com/{tid}/v2.0",
            "aud": aud or MS_ID, "sub": sub, "email": email, "tid": tid,
            "nonce": nonce, "exp": time.time() + 300}


def non_verificata(conn, email) -> bool | None:
    with conn.cursor() as cur:
        cur.execute("SELECT email_verified_at IS NULL FROM users WHERE email = %s", (email,))
        r = cur.fetchone()
        return r[0] if r else None


def utente_di(conn, email) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT id::text FROM users WHERE email = %s", (email,))
        r = cur.fetchone()
        return r[0] if r else None


def valore(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        r = cur.fetchone()
        return r[0] if r else None


RICHIESTE_GDPR: list[str] = []


def verifica_revoca_microsoft_legacy() -> None:
    """La 0084 chiude il perimetro legacy senza toccare gli altri account."""
    migrazione = (ROOT / "migrations" / "0084_revoca_microsoft_legacy.sql").read_text()
    with psycopg.connect(database_url()) as conn:
        try:
            legacy_email = "oauth-check-revoca-legacy@esempio.test"
            sicura_email = "oauth-check-revoca-sicura@esempio.test"
            auth.richiedi_magic_link(conn, legacy_email, invia=lambda *a: None)
            auth.richiedi_magic_link(conn, sicura_email, invia=lambda *a: None)
            legacy_uid = utente_di(conn, legacy_email)
            sicuro_uid = utente_di(conn, sicura_email)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO oauth_identities (provider, subject, user_id, email_at_link) "
                    "VALUES ('microsoft', 'm-0084-legacy', %s, %s)",
                    (legacy_uid, legacy_email))
                cur.execute(
                    "INSERT INTO oauth_identities (provider, subject, user_id, email_at_link, "
                    "link_method, tenant_id) VALUES ('microsoft', 'm-0084-sicuro', %s, %s, "
                    "'email', %s)", (sicuro_uid, sicura_email, MSA))
                for uid in (legacy_uid, sicuro_uid):
                    for origine in ("microsoft", "magic_link"):
                        cur.execute(
                            "INSERT INTO sessions (user_id, token_hash, expires_at, origin) "
                            "VALUES (%s, %s, now() + interval '30 days', %s)",
                            (uid, auth._sha256(f"0084-sessione-{uid}-{origine}"), origine))
                        cur.execute(
                            "INSERT INTO login_tokens (user_id, token_hash, expires_at, origin) "
                            "VALUES (%s, %s, now() + interval '5 minutes', %s)",
                            (uid, auth._sha256(f"0084-token-{uid}-{origine}"), origine))
                    cur.execute(
                        "INSERT INTO api_chiavi (key_hash, label, user_id, crediti_mensili, "
                        "usati_mese) VALUES (%s, %s, %s, 5000, 125)",
                        (auth._sha256(f"0084-chiave-{uid}"), f"0084-{uid}", uid))
                cur.execute(
                    "INSERT INTO api_chiavi (key_hash, label) VALUES (%s, '0084-interna')",
                    (auth._sha256("0084-chiave-interna"),))
                cur.execute("UPDATE users SET crediti_extra = 321 WHERE id = %s", (legacy_uid,))
            with conn.cursor() as cur:
                cur.execute(migrazione)
                cur.execute(migrazione)

            def revocata(sql: str, uid: str) -> bool | None:
                return valore(conn, sql, (uid,))

            check("0084: identita' Microsoft legacy revocata", revocata(
                "SELECT revoked_at IS NOT NULL FROM oauth_identities WHERE user_id = %s",
                legacy_uid), True)
            check("0084: sessione Microsoft legacy revocata", revocata(
                "SELECT revoked_at IS NOT NULL FROM sessions "
                "WHERE user_id = %s AND origin = 'microsoft'", legacy_uid), True)
            check("0084: token Microsoft legacy consumato", revocata(
                "SELECT consumed_at IS NOT NULL FROM login_tokens "
                "WHERE user_id = %s AND origin = 'microsoft'", legacy_uid), True)
            check("0084: chiave API legacy revocata", revocata(
                "SELECT revoked_at IS NOT NULL FROM api_chiavi WHERE user_id = %s",
                legacy_uid), True)
            check("0084: sessione magic-link conservata", revocata(
                "SELECT revoked_at IS NULL FROM sessions "
                "WHERE user_id = %s AND origin = 'magic_link'", legacy_uid), True)
            check("0084: token magic-link conservato", revocata(
                "SELECT consumed_at IS NULL FROM login_tokens "
                "WHERE user_id = %s AND origin = 'magic_link'", legacy_uid), True)
            check("0084: volume e consumo non azzerati", revocata(
                "SELECT crediti_mensili = 5000 AND usati_mese = 125 "
                "FROM api_chiavi WHERE user_id = %s", legacy_uid), True)
            check("0084: crediti extra non azzerati", revocata(
                "SELECT crediti_extra = 321 FROM users WHERE id = %s", legacy_uid), True)
            check("0084: identita' Microsoft non legacy conservata", revocata(
                "SELECT revoked_at IS NULL FROM oauth_identities WHERE user_id = %s",
                sicuro_uid), True)
            check("0084: sessione Microsoft non legacy conservata", revocata(
                "SELECT revoked_at IS NULL FROM sessions "
                "WHERE user_id = %s AND origin = 'microsoft'", sicuro_uid), True)
            check("0084: token Microsoft non legacy conservato", revocata(
                "SELECT consumed_at IS NULL FROM login_tokens "
                "WHERE user_id = %s AND origin = 'microsoft'", sicuro_uid), True)
            check("0084: chiave API non legacy conservata", revocata(
                "SELECT revoked_at IS NULL FROM api_chiavi WHERE user_id = %s",
                sicuro_uid), True)
            check("0084: chiave interna conservata", valore(
                conn, "SELECT user_id IS NULL AND revoked_at IS NULL "
                      "FROM api_chiavi WHERE label = '0084-interna'"), True)
            check("0084: riesecuzione senza duplicare revoche", valore(
                conn, "SELECT count(*) FROM oauth_identities "
                      "WHERE subject IN ('m-0084-legacy', 'm-0084-sicuro') "
                      "AND revoked_at IS NOT NULL"), 1)
        finally:
            conn.rollback()


def main() -> int:
    db = database_name()
    if not (db.endswith(("_test", "_dev")) or os.environ.get("NIVULT_ALLOW_DESTRUCTIVE") == "1"):
        print(f"rifiuto di girare su '{db}': serve un database _test/_dev.")
        return 2
    print(f"verifica OAuth — {safe_dsn(database_url())}")

    os.environ["GOOGLE_CLIENT_ID"] = GOOGLE_ID
    os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret"
    os.environ["MICROSOFT_CLIENT_ID"] = MS_ID
    os.environ["MICROSOFT_CLIENT_SECRET"] = "test-secret"

    emails = [f"oauth-check-{n}@esempio.test" for n in range(6)]
    emails.append("oauth-check-native@esempio.test")
    mavult_email = "oauth-check-mavult@esempio.test"
    exp_email = "oauth-check-exp@esempio.test"
    prehijack_email = "oauth-check-prehijack@esempio.test"
    altro_email = "oauth-check-altro@esempio.test"
    enterprise_email = "oauth-check-aziendale@esempio.test"
    gdpr_email = "oauth-check-gdpr@esempio.test"
    gdpr_legacy_email = "oauth-check-gdpr-legacy@esempio.test"
    emails.extend([mavult_email, exp_email, prehijack_email, altro_email,
                   enterprise_email, gdpr_email, gdpr_legacy_email])

    with psycopg.connect(database_url()) as conn:
        pulisci(conn, emails)

        print("\n— lo state —")
        state, nonce = avvia(conn, "google")
        g = esito(conn, "google", "code-1", state, claim_google("g-1", emails[0], nonce=nonce))
        check("primo consumo riesce", g.startswith("errore:"), False)
        g2 = esito(conn, "google", "code-1", state, claim_google("g-1", emails[0], nonce=nonce))
        check("stato monouso", g2, "errore:state_rifiutato")

        state, nonce = avvia(conn, "google")
        with conn.cursor() as cur:
            # created_at va spostato con expires_at: il vincolo della tabella
            # pretende che la finestra resti coerente anche invecchiandola.
            cur.execute("UPDATE oauth_flows "
                        "SET created_at = now() - make_interval(mins => 20), "
                        "    expires_at = now() - make_interval(mins => 1) "
                        "WHERE state_hash = %s", (oauth._sha256(state),))
        conn.commit()
        check("stato scaduto rifiutato",
              esito(conn, "google", "c", state, claim_google("g-x", emails[1], nonce=nonce)),
              "errore:state_rifiutato")

        state, nonce = avvia(conn, "google")
        check("stato di un provider non vale sull'altro",
              esito(conn, "microsoft", "c", state, claim_microsoft("m-x", emails[1], nonce=nonce)),
              "errore:state_rifiutato")

        print("\n— i claim dell'id_token —")
        state, nonce = avvia(conn, "google")
        check("nonce che non corrisponde",
              esito(conn, "google", "c", state, claim_google("g-9", emails[1], nonce="altro")),
              "errore:nonce_inatteso")

        state, nonce = avvia(conn, "google")
        check("aud di un'altra applicazione",
              esito(conn, "google", "c", state,
                    claim_google("g-9", emails[1], nonce=nonce, aud="app-di-qualcun-altro")),
              "errore:aud_inatteso")

        state, nonce = avvia(conn, "google")
        cattivo = claim_google("g-9", emails[1], nonce=nonce)
        cattivo["iss"] = "https://accounts.google.com.evil.test"
        check("emittente inatteso", esito(conn, "google", "c", state, cattivo),
              "errore:iss_inatteso")

        state, nonce = avvia(conn, "microsoft")
        storto = claim_microsoft("m-9", emails[1], nonce=nonce)
        storto["tid"] = "un-tenant-diverso-da-quello-nell-iss"
        check("tenant che non combacia con l'emittente",
              esito(conn, "microsoft", "c", state, storto), "errore:iss_inatteso")

        state, nonce = avvia(conn, "google")
        check("provider che rifiuta lo scambio",
              esito(conn, "google", "c", state, None, status=400), "errore:scambio_fallito")

        print("\n— la scadenza OIDC —")
        assente = object()
        for label, valore_exp in [
                ("mancante", assente), ("non numerica", "1730000000"),
                ("booleana", True), ("NaN", float("nan")),
                ("infinita", float("inf"))]:
            state, nonce = avvia(conn, "google")
            cattivo = claim_google("g-exp", exp_email, nonce=nonce)
            if valore_exp is assente:
                cattivo.pop("exp")
            else:
                cattivo["exp"] = valore_exp
            check(f"exp {label} rifiutata",
                  esito(conn, "google", "c", state, cattivo), "errore:exp_malformato")

        state, nonce = avvia(conn, "google")
        cattivo = claim_google("g-exp", exp_email, nonce=nonce)
        cattivo["exp"] = time.time() - 3600
        check("exp scaduta ha un errore distinto",
              esito(conn, "google", "c", state, cattivo), "errore:exp_scaduto")

        state, nonce = avvia(conn, "google")
        valido = claim_google("g-exp", exp_email, nonce=nonce)
        valido["exp"] = time.time() + 120
        gettone_exp = esito(conn, "google", "c", state, valido)
        check("exp numerica finita e valida accettata",
              bool(gettone_exp) and not gettone_exp.startswith("errore:"), True)

        print("\n— la politica di collegamento —")
        primo = utente_di(conn, emails[0])
        check("l'identità nota ritrova il suo utente", bool(primo), True)

        state, nonce = avvia(conn, "google")
        esito(conn, "google", "c", state, claim_google("g-1", emails[0], nonce=nonce))
        check("stessa identità, stesso utente", utente_di(conn, emails[0]), primo)

        # Utente nato dal magic link, poi si presenta Google con email verificata.
        auth.richiedi_magic_link(conn, emails[2], invia=lambda *a: None)
        atteso = utente_di(conn, emails[2])
        state, nonce = avvia(conn, "google")
        esito(conn, "google", "c", state, claim_google("g-2", emails[2], nonce=nonce))
        check("Google verificata collega all'account esistente",
              utente_di(conn, emails[2]), atteso)

        # Stessa situazione, ma l'email NON è verificata: non si collega.
        auth.richiedi_magic_link(conn, emails[3], invia=lambda *a: None)
        state, nonce = avvia(conn, "google")
        check("Google non verificata NON collega",
              esito(conn, "google", "c", state,
                    claim_google("g-3", emails[3], verificata=False, nonce=nonce)),
              "errore:collegamento_non_provato")

        # Il caso nOAuth: l'account esiste ed ha già l'email verificata.
        link_esistente = auth.richiedi_magic_link(conn, emails[4], invia=lambda *a: None)
        auth.consuma(conn, link_esistente)
        check("fixture nOAuth: account esistente con email verificata",
              non_verificata(conn, emails[4]), False)
        state, nonce = avvia(conn, "microsoft")
        check("tenant aziendale NON collega un account esistente",
              esito(conn, "microsoft", "c", state,
                    claim_microsoft("m-3", emails[4], tid=TENANT_AZIENDALE, nonce=nonce)),
              "errore:collegamento_autenticato_richiesto")

        # Account personale Microsoft: lì l'email è l'identificativo, e vale.
        auth.richiedi_magic_link(conn, emails[5], invia=lambda *a: None)
        atteso = utente_di(conn, emails[5])
        state, nonce = avvia(conn, "microsoft")
        esito(conn, "microsoft", "c", state, claim_microsoft("m-4", emails[5], nonce=nonce))
        check("account personale Microsoft collega", utente_di(conn, emails[5]), atteso)

        print("\n— la sessione che ne nasce —")
        state, nonce = avvia(conn, "google")
        gettone = esito(conn, "google", "c", state, claim_google("g-1", emails[0], nonce=nonce))
        r = auth.consuma(conn, gettone)
        check("il gettone si scambia con una sessione", bool(r), True)
        with conn.cursor() as cur:
            cur.execute("SELECT origin FROM sessions WHERE user_id = %s "
                        "ORDER BY created_at DESC LIMIT 1", (primo,))
            check("la sessione è marcata 'google'", cur.fetchone()[0], "google")
        check("il gettone vale una volta sola", auth.consuma(conn, gettone), None)

        print("\n— il giro HTTP nativo —")
        verifier = "v" * 43
        challenge = sfida_nativa(verifier)
        risposta_provider = {"claim": None, "status": 200}
        concludi_reale = oauth.concludi

        def concludi_con_client(conn_, provider, code, state):
            return concludi_reale(
                conn_, provider, code, state,
                client=ClientFinto(risposta_provider["claim"],
                                   risposta_provider["status"]))

        oauth.concludi = concludi_con_client
        sito_precedente = os.environ.get("SITE_URL")
        os.environ["SITE_URL"] = "https://nivult.com"
        try:
            with TestClient(create_app()) as client:
                def start_nativo(stato):
                    return client.get(
                        "/auth/oauth/google/start",
                        params={"dest": oauth.NATIVE_CALLBACK,
                                "native_state": stato,
                                "native_challenge": challenge},
                        follow_redirects=False)

                def parti_redirect(r):
                    u = urlparse(r.headers.get("location", ""))
                    return u, parse_qs(u.query)

                r = start_nativo("app-success")
                check("start nativo: 302 al provider", r.status_code, 302)
                q = parse_qs(urlparse(r.headers["location"]).query)
                backend_state = q["state"][0]
                check("lo state nativo non va al provider", "native_state" in q, False)
                check("la sfida nativa non va al provider",
                      "native_challenge" in q, False)
                check("il PKCE del provider resta separato",
                      q["code_challenge"][0] != challenge, True)

                callback_precedente = oauth.NATIVE_CALLBACK
                oauth.NATIVE_CALLBACK = "mavult://oauth/callback"
                try:
                    r_mavult = start_nativo("app-mavult")
                    check("start nativo Mavult: 302 al provider",
                          r_mavult.status_code, 302)
                    q_mavult = parse_qs(urlparse(r_mavult.headers["location"]).query)
                    risposta_provider["claim"] = claim_google(
                        "g-mavult", mavult_email, nonce=q_mavult["nonce"][0])
                    r_mavult = client.get(
                        "/auth/oauth/google/callback",
                        params={"code": "code-mavult", "state": q_mavult["state"][0]},
                        follow_redirects=False)
                    u_mavult, q_mavult = parti_redirect(r_mavult)
                    check("callback Mavult completo sul deep link configurato",
                          (r_mavult.status_code, u_mavult.scheme,
                           u_mavult.netloc, u_mavult.path),
                          (302, "mavult", "oauth", "/callback"))
                    check("callback Mavult conserva lo state dell'app",
                          q_mavult.get("native_state"), ["app-mavult"])
                    r_mavult = client.post(
                        "/auth/consuma",
                        json={"token": q_mavult["token"][0],
                              "native_verifier": verifier})
                    check("gettone Mavult consumato con sessione",
                          r_mavult.status_code, 200)
                    check("risposta Mavult contiene sessione e utente",
                          bool(r_mavult.json().get("sessione"))
                          and bool(r_mavult.json().get("utente")), True)
                finally:
                    oauth.NATIVE_CALLBACK = callback_precedente

                risposta_provider["claim"] = claim_google(
                    "g-native", emails[6], nonce=q["nonce"][0])
                r = client.get(
                    "/auth/oauth/google/callback",
                    params={"code": "code-native", "state": backend_state,
                            "dest": "https://malevole.example"},
                    follow_redirects=False)
                check("callback nativo: 302", r.status_code, 302)
                u, q = parti_redirect(r)
                check("successo sul deep link esatto",
                      (u.scheme, u.netloc, u.path, u.fragment),
                      ("nivult", "oauth", "/callback", ""))
                check("ritorna lo state nativo originale",
                      q.get("native_state"), ["app-success"])
                token_nativo = q["token"][0]

                check("il token nativo senza verifier e' rifiutato",
                      client.post("/auth/consuma", json={"token": token_nativo})
                      .status_code, 401)
                check("un verifier sbagliato e' rifiutato",
                      client.post("/auth/consuma",
                                  json={"token": token_nativo,
                                        "native_verifier": "x" * 43}).status_code,
                      401)
                r = client.post("/auth/consuma",
                                json={"token": token_nativo,
                                      "native_verifier": verifier})
                check("il verifier giusto apre la sessione", r.status_code, 200)
                check("la risposta nativa ha sessione e utente",
                      bool(r.json().get("sessione")) and bool(r.json().get("utente")),
                      True)
                check("il token nativo non e' rigiocabile",
                      client.post("/auth/consuma",
                                  json={"token": token_nativo,
                                        "native_verifier": verifier}).status_code,
                      401)

                r = client.get(
                    "/auth/oauth/google/start",
                    params={"dest": "https://nivult.com/app/",
                            "native_state": "web-app",
                            "native_challenge": challenge},
                    follow_redirects=False)
                check("start app web: 302 al provider", r.status_code, 302)
                q = parse_qs(urlparse(r.headers["location"]).query)
                risposta_provider["claim"] = claim_google(
                    "g-web-app", "web-app@example.test", nonce=q["nonce"][0])
                r = client.get(
                    "/auth/oauth/google/callback",
                    params={"code": "code-web-app", "state": q["state"][0]},
                    follow_redirects=False)
                u, q = parti_redirect(r)
                check("callback app web resta su /app/",
                      (r.status_code, u.scheme, u.netloc, u.path),
                      (302, "https", "nivult.com", "/app/"))
                check("callback app web conserva state", q.get("native_state"), ["web-app"])
                r = client.post("/auth/consuma",
                                json={"token": q["token"][0], "native_verifier": verifier})
                check("app web consuma il token con PKCE", r.status_code, 200)
                check("callback app web esterna rifiutata",
                      client.get("/auth/oauth/google/start",
                                 params={"dest": "https://malevole.example/app/",
                                         "native_state": "web-app",
                                         "native_challenge": challenge},
                                 follow_redirects=False).status_code, 400)
                check("callback app web senza parametri nativi rifiutata",
                      client.get("/auth/oauth/google/start",
                                 params={"dest": "https://nivult.com/app/"},
                                 follow_redirects=False).status_code, 400)
                check("callback app web localhost consentita",
                      oauth.callback_nativa("http://localhost:8081/app/"), True)

                r = start_nativo("app-cancel")
                q = parse_qs(urlparse(r.headers["location"]).query)
                r = client.get(
                    "/auth/oauth/google/callback",
                    params={"error": "access_denied", "state": q["state"][0],
                            "dest": "https://malevole.example"},
                    follow_redirects=False)
                u, q = parti_redirect(r)
                check("annullamento sul deep link salvato",
                      (r.status_code, u.scheme, u.netloc, u.path),
                      (302, "nivult", "oauth", "/callback"))
                check("annullamento con errore e state originali",
                      (q.get("error"), q.get("native_state"), "token" in q),
                      (["access_denied"], ["app-cancel"], False))

                r = start_nativo("app-failure")
                q = parse_qs(urlparse(r.headers["location"]).query)
                risposta_provider["claim"] = None
                risposta_provider["status"] = 400
                r = client.get(
                    "/auth/oauth/google/callback",
                    params={"code": "code-native", "state": q["state"][0],
                            "dest": "https://malevole.example"},
                    follow_redirects=False)
                u, q = parti_redirect(r)
                check("fallimento sul deep link salvato",
                      (r.status_code, u.scheme, u.netloc, u.path),
                      (302, "nivult", "oauth", "/callback"))
                check("fallimento con errore e state originali",
                      (q.get("error"), q.get("native_state")),
                      (["scambio_fallito"], ["app-failure"]))
                risposta_provider["status"] = 200

                cattive = [
                    "https://nivult.com.malevole.example/account",
                    "https://nivult.com@malevole.example/account",
                    "https://utente@nivult.com/account",
                    "https://nivult.com/account#frammento",
                    "http://localhost.malevole.example/account",
                    "javascript:alert(1)",
                    "nivult://oauth/callback/",
                    " nivult://oauth/callback ",
                    "nivult://oauth/callback#frammento",
                ]
                for destinazione in cattive:
                    r = client.get("/auth/oauth/google/start",
                                   params={"dest": destinazione},
                                   follow_redirects=False)
                    check(f"destinazione rifiutata: {destinazione}",
                          r.status_code, 400)
                check("deep link senza parametri nativi rifiutato",
                      client.get("/auth/oauth/google/start",
                                 params={"dest": oauth.NATIVE_CALLBACK},
                                 follow_redirects=False).status_code, 400)
                check("sfida nativa malformata rifiutata",
                      client.get("/auth/oauth/google/start",
                                 params={"dest": oauth.NATIVE_CALLBACK,
                                         "native_state": "app",
                                         "native_challenge": "non-base64"},
                                 follow_redirects=False).status_code, 400)
                check("parametri nativi su una destinazione web rifiutati",
                      client.get("/auth/oauth/google/start",
                                 params={"dest": "https://nivult.com/account.html",
                                         "native_state": "app",
                                         "native_challenge": challenge},
                                 follow_redirects=False).status_code, 400)

                r = client.get(
                    "/auth/oauth/google/start",
                    params={"dest": "https://nivult.com/account.html"},
                    follow_redirects=False)
                check("destinazione B2B esistente ancora ammessa",
                      r.status_code, 302)
                q = parse_qs(urlparse(r.headers["location"]).query)
                risposta_provider["claim"] = claim_google(
                    "g-native", emails[6], nonce=q["nonce"][0])
                r = client.get(
                    "/auth/oauth/google/callback",
                    params={"code": "code-web", "state": q["state"][0]},
                    follow_redirects=False)
                u, q = parti_redirect(r)
                check("callback B2B ancora sulla pagina richiesta",
                      (r.status_code, u.scheme, u.netloc, u.path),
                      (302, "https", "nivult.com", "/account.html"))
                r = client.post("/auth/consuma", json={"token": q["token"][0]})
                check("il token web senza verifier continua a funzionare",
                      r.status_code, 200)

                r = client.get("/auth/oauth/google/start", follow_redirects=False)
                q = parse_qs(urlparse(r.headers["location"]).query)
                risposta_provider["claim"] = claim_google(
                    "g-native", emails[6], nonce=q["nonce"][0])
                r = client.get(
                    "/auth/oauth/google/callback",
                    params={"code": "code-web-default", "state": q["state"][0]},
                    follow_redirects=False)
                u, q = parti_redirect(r)
                check("callback web predefinito ancora su /verify",
                      (r.status_code, u.scheme, u.netloc, u.path, "token" in q),
                      (302, "https", "nivult.com", "/verify", True))

                r = client.get(
                    "/auth/oauth/google/start",
                    params={"dest": "http://localhost:3000/account.html"},
                    follow_redirects=False)
                check("destinazione localhost esistente ancora ammessa",
                      r.status_code, 302)
        finally:
            oauth.concludi = concludi_reale
            if sito_precedente is None:
                os.environ.pop("SITE_URL", None)
            else:
                os.environ["SITE_URL"] = sito_precedente

        print("\n— Microsoft enterprise: pre-hijack chiuso —")
        state, nonce = avvia(conn, "microsoft")
        check("enterprise sconosciuto non pre-crea l'account",
              esito(conn, "microsoft", "c", state,
                    claim_microsoft("m-9", enterprise_email,
                                    tid=TENANT_AZIENDALE, nonce=nonce)),
              "errore:collegamento_autenticato_richiesto")
        check("dopo il tentativo l'account non esiste",
              utente_di(conn, enterprise_email), None)

        link_vittima = auth.richiedi_magic_link(
            conn, prehijack_email, invia=lambda *a: None)
        vittima = utente_di(conn, prehijack_email)
        token_attaccante = "oauth-token-microsoft-legacy"
        sessione_attaccante = "oauth-sessione-microsoft-legacy"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO oauth_identities (provider, subject, user_id, email_at_link) "
                "VALUES ('microsoft', 'm-legacy-prehijack', %s, %s)",
                (vittima, prehijack_email))
            cur.execute(
                "INSERT INTO login_tokens (user_id, token_hash, expires_at, origin) "
                "VALUES (%s, %s, now() + interval '5 minutes', 'microsoft')",
                (vittima, auth._sha256(token_attaccante)))
            cur.execute(
                "INSERT INTO sessions (user_id, token_hash, expires_at, origin) "
                "VALUES (%s, %s, now() + interval '30 days', 'microsoft')",
                (vittima, auth._sha256(sessione_attaccante)))
        conn.commit()
        sessione_vittima, uid_vittima = auth.consuma(conn, link_vittima)
        check("la vittima entra col magic link sull'account pre-creato",
              uid_vittima, vittima)
        check("la vecchia sessione Microsoft era valida prima della revoca",
              auth.verifica_sessione(conn, sessione_attaccante), vittima)

        state, nonce = avvia(conn, "microsoft")
        check("il successivo login enterprise legacy viene rifiutato",
              esito(conn, "microsoft", "c", state,
                    claim_microsoft("m-legacy-prehijack", prehijack_email,
                                    tid=TENANT_AZIENDALE, nonce=nonce)),
              "errore:identita_enterprise_revocata")
        check("l'identità legacy risulta revocata",
              valore(conn, "SELECT revoked_at IS NOT NULL FROM oauth_identities "
                     "WHERE provider = 'microsoft' AND subject = 'm-legacy-prehijack'"),
              True)
        check("il tenant osservato resta registrato",
              valore(conn, "SELECT tenant_id FROM oauth_identities "
                     "WHERE provider = 'microsoft' AND subject = 'm-legacy-prehijack'"),
              TENANT_AZIENDALE)
        check("la sessione Microsoft legacy è chiusa",
              auth.verifica_sessione(conn, sessione_attaccante), None)
        check("il token Microsoft legacy non è più consumabile",
              auth.consuma(conn, token_attaccante), None)
        check("non restano token Microsoft aperti né nuovi",
              valore(conn, "SELECT count(*) FROM login_tokens "
                     "WHERE user_id = %s AND origin = 'microsoft' AND consumed_at IS NULL",
                     (vittima,)), 0)
        check("la sessione magic-link della vittima resta valida",
              auth.verifica_sessione(conn, sessione_vittima), vittima)

        print("\n— collegamento autenticato e login successivo —")
        subject_recuperato = "m-vittima-recuperata"
        state, nonce = avvia(conn, "microsoft", link_user_id=vittima)
        check("lo state conserva il solo utente autenticato",
              valore(conn, "SELECT link_user_id::text FROM oauth_flows "
                     "WHERE state_hash = %s", (oauth._sha256(state),)), vittima)
        prima = (
            valore(conn, "SELECT count(*) FROM login_tokens WHERE user_id = %s", (vittima,)),
            valore(conn, "SELECT count(*) FROM sessions WHERE user_id = %s", (vittima,)),
        )
        check("il callback di collegamento non restituisce un gettone",
              esito(conn, "microsoft", "c", state,
                    claim_microsoft(subject_recuperato, prehijack_email,
                                    tid=TENANT_AZIENDALE, nonce=nonce)), None)
        dopo = (
            valore(conn, "SELECT count(*) FROM login_tokens WHERE user_id = %s", (vittima,)),
            valore(conn, "SELECT count(*) FROM sessions WHERE user_id = %s", (vittima,)),
        )
        check("il callback di collegamento non crea token né sessioni", dopo, prima)
        check("la vittima sostituisce il subject legacy col proprio",
              valore(conn, "SELECT link_method FROM oauth_identities "
                     "WHERE provider = 'microsoft' AND subject = %s",
                     (subject_recuperato,)), "authenticated")
        check("il collegamento autenticato riattiva l'identità",
              valore(conn, "SELECT revoked_at IS NULL FROM oauth_identities "
                     "WHERE provider = 'microsoft' AND subject = %s",
                     (subject_recuperato,)), True)

        state, nonce = avvia(conn, "microsoft")
        gettone = esito(conn, "microsoft", "c", state,
                        claim_microsoft(subject_recuperato, prehijack_email,
                                        tid=TENANT_AZIENDALE, nonce=nonce))
        check("dopo il collegamento il login enterprise emette un gettone",
              bool(gettone) and not gettone.startswith("errore:"), True)
        r = auth.consuma(conn, gettone)
        check("il login successivo apre la sessione dello stesso utente",
              r[1] if r else None, vittima)

        link_altro = auth.richiedi_magic_link(conn, altro_email, invia=lambda *a: None)
        _, altro_uid = auth.consuma(conn, link_altro)
        state, nonce = avvia(conn, "microsoft", link_user_id=altro_uid)
        check("un'identità di un altro utente non è sottraibile",
              esito(conn, "microsoft", "c", state,
                    claim_microsoft(subject_recuperato, prehijack_email,
                                    tid=TENANT_AZIENDALE, nonce=nonce)),
              "errore:identita_gia_collegata")
        check("il proprietario dell'identità non cambia",
              valore(conn, "SELECT user_id::text FROM oauth_identities "
                     "WHERE provider = 'microsoft' AND subject = %s",
                     (subject_recuperato,)), vittima)

        print("\n— il collegamento enterprise non verifica l'email —")
        nuovo = enterprise_email
        link = auth.richiedi_magic_link(conn, nuovo, invia=lambda *a: None)
        _, nuovo_uid = auth.consuma(conn, link)
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET email_verified_at = NULL WHERE id = %s",
                        (nuovo_uid,))
        conn.commit()
        state, nonce = avvia(conn, "microsoft", link_user_id=nuovo_uid)
        check("anche un enterprise nuovo si collega da sessione autenticata",
              esito(conn, "microsoft", "c", state,
                    claim_microsoft("m-9", nuovo,
                                    tid=TENANT_AZIENDALE, nonce=nonce)), None)
        check("il collegamento non promuove l'email", non_verificata(conn, nuovo), True)
        state, nonce = avvia(conn, "microsoft")
        gettone = esito(conn, "microsoft", "c", state,
                        claim_microsoft("m-9", nuovo,
                                        tid=TENANT_AZIENDALE, nonce=nonce))
        sessione_enterprise, uid_enterprise = auth.consuma(conn, gettone)
        check("il login enterprise successivo funziona", uid_enterprise, nuovo_uid)
        check("nemmeno il consumo OAuth promuove l'email",
              non_verificata(conn, nuovo), True)

        tenant_successivo = "22222222-3333-4444-5555-666666666666"
        state, nonce = avvia(conn, "microsoft", link_user_id=nuovo_uid)
        check("il ricollegamento su un tenant diverso è autenticato",
              esito(conn, "microsoft", "c", state,
                    claim_microsoft("m-9", nuovo,
                                    tid=tenant_successivo, nonce=nonce)), None)
        check("il cambio tenant chiude la sessione Microsoft precedente",
              auth.verifica_sessione(conn, sessione_enterprise), None)
        check("il tenant nuovo sostituisce il precedente",
              valore(conn, "SELECT tenant_id FROM oauth_identities "
                     "WHERE provider = 'microsoft' AND subject = 'm-9'"),
              tenant_successivo)
        link = auth.richiedi_magic_link(conn, nuovo, invia=lambda *a: None)
        auth.consuma(conn, link)
        check("il magic link invece promuove l'email", non_verificata(conn, nuovo), False)

        print("\n— revoca immediata e cancellazione GDPR —")
        link = auth.richiedi_magic_link(conn, gdpr_email, invia=lambda *a: None)
        sessione_gdpr, uid_gdpr = auth.consuma(conn, link)
        check("sessione da invalidare già usata (cache calda)",
              auth.verifica_sessione(conn, sessione_gdpr), uid_gdpr)
        token_aperto = auth.richiedi_magic_link(conn, gdpr_email, invia=lambda *a: None)
        hash_chiave = hashlib.sha256(b"oauth-check-chiave-utente").hexdigest()
        hash_interna = hashlib.sha256(b"oauth-check-chiave-interna").hexdigest()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_chiavi (key_hash, label, user_id) VALUES (%s, %s, %s)",
                (hash_chiave, "oauth-check-utente", uid_gdpr))
            cur.execute(
                "INSERT INTO api_chiavi (key_hash, label) VALUES (%s, 'oauth-check-interna')",
                (hash_interna,))
        conn.commit()
        richiesta = gdpr.request_deletion(conn, uid_gdpr)
        RICHIESTE_GDPR.append(richiesta)

        with psycopg.connect(database_url()) as altra:
            check("seconda connessione: sessione revocata alla richiesta",
                  valore(altra, "SELECT revoked_at IS NOT NULL FROM sessions "
                         "WHERE user_id = %s", (uid_gdpr,)), True)
            check("seconda connessione: login token aperto revocato",
                  valore(altra, "SELECT consumed_at IS NOT NULL FROM login_tokens "
                         "WHERE user_id = %s AND token_hash = %s",
                         (uid_gdpr, auth._sha256(token_aperto))), True)
            check("seconda connessione: API key revocata alla richiesta",
                  valore(altra, "SELECT revoked_at IS NOT NULL FROM api_chiavi "
                         "WHERE key_hash = %s", (hash_chiave,)), True)
            check("la chiave interna NULL resta attiva",
                  valore(altra, "SELECT user_id IS NULL AND revoked_at IS NULL "
                         "FROM api_chiavi WHERE key_hash = %s", (hash_interna,)), True)
            check("la sessione deleted non passa più",
                  auth.verifica_sessione(altra, sessione_gdpr), None)
            check("il token emesso prima della cancellazione non passa",
                  auth.consuma(altra, token_aperto), None)
            inviate = []
            check("nessun nuovo magic link per un utente deleted",
                  auth.richiedi_magic_link(
                      altra, gdpr_email,
                      invia=lambda *a: inviate.append(a)), None)
            check("nessuna email viene spedita all'account deleted", inviate, [])

        totali = gdpr.execute_deletion(conn, richiesta, batch_size=100)
        check("l'esecuzione elimina davvero l'utente", totali.get("users"), 1)
        with psycopg.connect(database_url()) as altra:
            check("ON DELETE CASCADE elimina la chiave dell'utente",
                  valore(altra, "SELECT count(*) FROM api_chiavi WHERE key_hash = %s",
                         (hash_chiave,)), 0)
            check("la chiave interna NULL sopravvive all'esecuzione",
                  valore(altra, "SELECT user_id IS NULL AND revoked_at IS NULL "
                         "FROM api_chiavi WHERE key_hash = %s", (hash_interna,)), True)

        link = auth.richiedi_magic_link(conn, gdpr_legacy_email, invia=lambda *a: None)
        sessione_legacy, uid_legacy = auth.consuma(conn, link)
        token_legacy = auth.richiedi_magic_link(
            conn, gdpr_legacy_email, invia=lambda *a: None)
        hash_legacy = hashlib.sha256(b"oauth-check-chiave-legacy").hexdigest()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_chiavi (key_hash, label, user_id) VALUES (%s, %s, %s)",
                (hash_legacy, "oauth-check-legacy", uid_legacy))
            cur.execute(
                "INSERT INTO deletion_requests (user_id) VALUES (%s) RETURNING id",
                (uid_legacy,))
            richiesta_legacy = str(cur.fetchone()[0])
        conn.commit()
        RICHIESTE_GDPR.append(richiesta_legacy)
        try:
            gdpr.execute_deletion(conn, richiesta_legacy, max_batches=0)
        except gdpr.DeletionError:
            guardia = True
        else:
            guardia = False
        check("la guardia legacy precede anche un'esecuzione interrotta", guardia, True)
        with psycopg.connect(database_url()) as altra:
            check("execute_deletion marca deleted una richiesta legacy",
                  valore(altra, "SELECT status FROM users WHERE id = %s",
                         (uid_legacy,)), "deleted")
            check("execute_deletion revoca la sessione legacy",
                  auth.verifica_sessione(altra, sessione_legacy), None)
            check("execute_deletion revoca il login token legacy",
                  auth.consuma(altra, token_legacy), None)
            check("execute_deletion revoca l'API key legacy",
                  valore(altra, "SELECT revoked_at IS NOT NULL FROM api_chiavi "
                         "WHERE key_hash = %s", (hash_legacy,)), True)

        print("\n— revoca delle credenziali Microsoft legacy (0084) —")
        verifica_revoca_microsoft_legacy()

        pulisci(conn, emails)

    print(f"\n{len(PASSED)} ok, {len(FAILED)} falliti")
    for f in FAILED:
        print(f"  - {f}")
    return 1 if FAILED else 0


def pulisci(conn, emails: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM deletion_requests WHERE id = ANY(%s) OR user_id IN "
            "(SELECT id FROM users WHERE email = ANY(%s))",
            (RICHIESTE_GDPR, emails))
        cur.execute(
            "DELETE FROM api_chiavi WHERE label IN "
            "('oauth-check-interna', 'oauth-check-utente', 'oauth-check-legacy')")
        cur.execute("DELETE FROM users WHERE email = ANY(%s)", (emails,))
        cur.execute("DELETE FROM oauth_flows")
    conn.commit()
    RICHIESTE_GDPR.clear()


if __name__ == "__main__":
    raise SystemExit(main())
