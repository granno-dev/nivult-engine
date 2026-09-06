#!/usr/bin/env python3
"""Verifica dell'accesso con Google e Microsoft.

    python scripts/check_oauth.py

Non parla con Google: il client HTTP è iniettato e l'id_token è costruito
qui. Ciò che si prova è quello che possiamo sbagliare noi — il consumo dello
state, la validazione dei claim, e soprattutto la politica di collegamento
degli account, che è il punto in cui un errore diventa un takeover.

DISTRUTTIVO: scrive utenti e poi ripulisce. Solo su database _test/_dev.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psycopg  # noqa: E402

from nivult import auth, oauth  # noqa: E402
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

    def json(self):
        return self._corpo


class ClientFinto:
    """Sta al posto di httpx: risponde con l'id_token che gli si dà."""

    def __init__(self, claim: dict | None = None, status: int = 200):
        self.claim = claim
        self.status = status

    def post(self, url, data=None):
        if self.claim is None:
            return Risposta(self.status, {"errore": "no"})
        return Risposta(self.status, {"id_token": id_token(self.claim)})


def avvia(conn, provider: str) -> tuple[str, str]:
    """Apre un giro e ne estrae state e nonce dalla URL di autorizzazione."""
    url = oauth.inizia(conn, provider)
    q = parse_qs(urlparse(url).query)
    return q["state"][0], q["nonce"][0]


def esito(conn, provider, code, state, claim, status=200) -> str:
    """concludi(), ma il codice d'errore al posto dell'eccezione."""
    try:
        return oauth.concludi(conn, provider, code, state,
                              client=ClientFinto(claim, status))
    except oauth.OAuthError as e:
        return f"errore:{e.codice}"


def claim_google(sub, email, verificata=True, nonce="", aud=None):
    return {"iss": "https://accounts.google.com", "aud": aud or GOOGLE_ID,
            "sub": sub, "email": email, "email_verified": verificata,
            "nonce": nonce}


def claim_microsoft(sub, email, tid=MSA, nonce="", aud=None):
    return {"iss": f"https://login.microsoftonline.com/{tid}/v2.0",
            "aud": aud or MS_ID, "sub": sub, "email": email, "tid": tid,
            "nonce": nonce}


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

        # Il caso nOAuth: tenant aziendale, email di un account che esiste già.
        auth.richiedi_magic_link(conn, emails[4], invia=lambda *a: None)
        state, nonce = avvia(conn, "microsoft")
        check("tenant aziendale NON collega un account esistente",
              esito(conn, "microsoft", "c", state,
                    claim_microsoft("m-3", emails[4], tid=TENANT_AZIENDALE, nonce=nonce)),
              "errore:collegamento_non_provato")

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

        print("\n— l'email verificata non si regala —")
        # Un indirizzo NUOVO da tenant aziendale: si crea (non collega), e
        # l'email resta non verificata perché nessuno ne ha provato il possesso.
        nuovo = "oauth-check-aziendale@esempio.test"
        pulisci(conn, [nuovo])
        state, nonce = avvia(conn, "microsoft")
        gettone = esito(conn, "microsoft", "c", state,
                        claim_microsoft("m-9", nuovo, tid=TENANT_AZIENDALE, nonce=nonce))
        check("account da tenant aziendale nasce con email NON verificata",
              non_verificata(conn, nuovo), True)

        # Il punto vero: consumare il gettone apre la sessione ma NON deve
        # promuovere l'email. Prima della correzione in auth.consuma, qui
        # l'indirizzo risultava verificato senza che nessuno l'avesse provato.
        check("il gettone aziendale apre comunque la sessione",
              bool(auth.consuma(conn, gettone)), True)
        check("ma il consumo NON promuove l'email", non_verificata(conn, nuovo), True)

        # Contro-prova: sullo stesso account il magic link, quello sì, la promuove.
        link = auth.richiedi_magic_link(conn, nuovo, invia=lambda *a: None)
        auth.consuma(conn, link)
        check("il magic link invece la promuove", non_verificata(conn, nuovo), False)

        pulisci(conn, emails + [nuovo])

    print(f"\n{len(PASSED)} ok, {len(FAILED)} falliti")
    for f in FAILED:
        print(f"  - {f}")
    return 1 if FAILED else 0


def pulisci(conn, emails: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE email = ANY(%s)", (emails,))
        cur.execute("DELETE FROM oauth_flows")
    conn.commit()


if __name__ == "__main__":
    raise SystemExit(main())
