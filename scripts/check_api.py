#!/usr/bin/env python3
"""Verifica dell'API HTTP: autenticazione, CORS, sessioni.

    python scripts/check_api.py

Come check_modules, ma attraverso HTTP: il TestClient di FastAPI parla
davvero all'app, con le sue dipendenze e i suoi codici di stato. L'invio
dell'email è intercettato: qui si prova il protocollo, non l'SMTP (quello
l'ha già provato il digest vero).

DISTRUTTIVO: scrive utenti e poi ripulisce. Solo su database _test/_dev.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from nivult import auth  # noqa: E402
from nivult.api.app import create_app  # noqa: E402
from nivult.config import database_name, database_url, safe_dsn  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, got, expected) -> None:
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}  ->  {got!r}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} — atteso {expected!r}, ottenuto {got!r}")


def main() -> int:
    db = database_name()
    if not (db.endswith(("_test", "_dev")) or os.environ.get("NIVULT_ALLOW_DESTRUCTIVE") == "1"):
        print(f"rifiuto di girare su '{db}': serve un database _test/_dev.")
        return 2
    print(f"verifica API — {safe_dsn(database_url())}")

    tokens: list[str | None] = []
    originale = auth.richiedi_magic_link

    def intercetta(conn, email, **kw):
        kw["invia"] = lambda destinatario, oggetto, testo, html: None
        tokens.append(originale(conn, email, **kw))
        return tokens[-1]

    auth.richiedi_magic_link = intercetta
    app = create_app()

    with psycopg.connect(database_url()) as pulizia:
        with pulizia.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
        pulizia.commit()

    try:
        with TestClient(app) as client:
            r = client.post("/auth/magic-link", json={"email": "Api@Test.dev"})
            check("richiesta link: 202 comunque vada", r.status_code, 202)
            check("email non valida rifiutata dal validator",
                  client.post("/auth/magic-link", json={"email": "non-una-email"})
                  .status_code, 422)

            # Il rate limit non si vede da fuori: quarta richiesta, stesso 202.
            for _ in range(3):
                client.post("/auth/magic-link", json={"email": "api@test.dev"})
            check("il rate limit non rivela nulla: sempre 202",
                  client.post("/auth/magic-link", json={"email": "api@test.dev"}).status_code,
                  202)

            r = client.post("/auth/consuma", json={"token": tokens[0]})
            check("il link si consuma via HTTP", r.status_code, 200)
            sessione = r.json().get("sessione", "")
            check("la risposta porta un token di sessione", len(sessione) > 30, True)

            check("il link non si usa due volte",
                  client.post("/auth/consuma", json={"token": tokens[0]}).status_code, 401)
            check("un token inventato non passa",
                  client.post("/auth/consuma", json={"token": "pippo"}).status_code, 401)

            check("/me senza sessione: 401", client.get("/me").status_code, 401)
            check("Bearer sbagliato: 401",
                  client.get("/me", headers={"Authorization": "Bearer pippo"}).status_code, 401)
            r = client.get("/me", headers={"Authorization": f"Bearer {sessione}"})
            check("/me con sessione valida: 200", r.status_code, 200)
            check("l'utente è quello del link",
                  r.json().get("email"), "api@test.dev")

            r = client.post("/auth/logout",
                            headers={"Authorization": f"Bearer {sessione}"})
            check("logout: 200", r.status_code, 200)
            check("dopo il logout la sessione è morta",
                  client.get("/me", headers={"Authorization": f"Bearer {sessione}"})
                  .status_code, 401)

            # --- vocabolari, cluster, preferenze --------------------------------
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("TRUNCATE users CASCADE")
                    cur.execute("SELECT count(*) FROM job_families")
                    if cur.fetchone()[0] == 0:
                        cur.execute("INSERT INTO job_families (family, sort_order) "
                                    "VALUES ('Human Resources', 1)")
                    cur.execute("INSERT INTO clusters (family, country) "
                                "VALUES ('Human Resources','IT') "
                                "ON CONFLICT (family, country) DO NOTHING")
                    cur.execute("SELECT id::text FROM clusters "
                                "WHERE family = 'Human Resources' AND country = 'IT'")
                    cluster_id = cur.fetchone()[0]
                setup.commit()

            client.post("/auth/magic-link", json={"email": "pref@test.dev"})
            r = client.post("/auth/consuma", json={"token": tokens[-1]})
            sessione = r.json()["sessione"]
            auth_header = {"Authorization": f"Bearer {sessione}"}

            r = client.get("/vocabolari")
            check("i vocabolari ci sono", r.status_code, 200)
            v = r.json()
            check("le lingue vengono dal vocabolario in tabella",
                  "Italian" in v["lingue"] and "French" in v["lingue"], True)
            check("i livelli di esperienza sono ordinati",
                  [l["codice"] for l in v["livelli_esperienza"]],
                  ["0-2", "2-5", "5-10", "10+"])

            r = client.get("/cluster")
            check("l'elenco dei cluster comprende HR × IT",
                  any(c["famiglia"] == "Human Resources" and c["paese"] == "IT"
                      for c in r.json()), True)

            filtri = {"languages": ["Italian", "English"], "min_seniority": "2-5",
                      "max_seniority": "10+", "employment_types": ["FULL_TIME"],
                      "min_headcount": 50}
            r = client.put(f"/me/cluster/{cluster_id}", json=filtri,
                           headers=auth_header)
            check("iscrizione al cluster con filtri: 204", r.status_code, 204)
            r = client.get("/me/cluster", headers=auth_header)
            check("l'iscrizione si rilegge con i filtri giusti",
                  (len(r.json()), r.json()[0]["filtri"]["languages"],
                   r.json()[0]["filtri"]["min_headcount"]),
                  (1, ["Italian", "English"], 50))
            check("i tipi datore accettati tornano al default tutti e tre",
                  r.json()[0]["filtri"]["accepted_employer_kinds"],
                  ["direct", "staffing_agency", "undisclosed"])

            bad = dict(filtri, languages=["Italianooo"])
            r = client.put(f"/me/cluster/{cluster_id}", json=bad, headers=auth_header)
            check("una lingua fuori vocabolario è rifiutata con 422",
                  r.status_code, 422)
            check("il 422 dice quali valori sono ammessi",
                  "Italian" in str(r.json()), True)
            bad = dict(filtri, min_seniority="senior")
            check("una seniority fuori vocabolario è rifiutata",
                  client.put(f"/me/cluster/{cluster_id}", json=bad,
                             headers=auth_header).status_code, 422)
            check("un cluster inesistente è un 404",
                  client.put("/me/cluster/00000000-0000-0000-0000-000000000000",
                             json=filtri, headers=auth_header).status_code, 404)

            check("disiscrizione: 204",
                  client.delete(f"/me/cluster/{cluster_id}",
                                headers=auth_header).status_code, 204)
            check("dopo la disiscrizione non ci sono cluster",
                  client.get("/me/cluster", headers=auth_header).json(), [])

            check("passare a daily azzera il weekday con un null esplicito",
                  client.put("/me", json={"frequency": "daily", "send_weekday": None},
                             headers=auth_header).status_code, 200)
            check("weekly senza giorno è rifiutato",
                  client.put("/me", json={"frequency": "weekly"},
                             headers=auth_header).status_code, 422)
            check("weekly col giorno passa",
                  client.put("/me", json={"frequency": "weekly", "send_weekday": 1},
                             headers=auth_header).status_code, 200)
            check("daily col giorno è rifiutato",
                  client.put("/me", json={"frequency": "daily", "send_weekday": 1},
                             headers=auth_header).status_code, 422)
            check("un fuso orario inesistente è rifiutato",
                  client.put("/me", json={"timezone": "Fuso/Fasullo"},
                             headers=auth_header).status_code, 422)
            check("il fuso valido passa e si rilegge",
                  client.put("/me", json={"timezone": "Europe/Rome"},
                             headers=auth_header).status_code, 200)
            r = client.get("/me", headers=auth_header)
            check("le preferenze salvate si vedono da /me",
                  (r.json()["frequenza"], r.json()["fuso"]), ("weekly", "Europe/Rome"))

            # CORS: solo gli origine dichiarati parlano con l'API.
            pre = client.options(
                "/auth/magic-link", headers={
                    "Origin": "https://nivult.com",
                    "Access-Control-Request-Method": "POST"})
            check("preflight da nivult.com accettato",
                  pre.headers.get("access-control-allow-origin"), "https://nivult.com")
            pre = client.options(
                "/auth/magic-link", headers={
                    "Origin": "https://malevole.example",
                    "Access-Control-Request-Method": "POST"})
            check("preflight da un origine estraneo rifiutato",
                  pre.headers.get("access-control-allow-origin"), None)
    finally:
        auth.richiedi_magic_link = originale

    with psycopg.connect(database_url()) as pulizia:
        with pulizia.cursor() as cur:
            cur.execute("TRUNCATE users CASCADE")
        pulizia.commit()

    print(f"\n{len(PASSED)} superati, {len(FAILED)} falliti")
    if FAILED:
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("OK: l'API autentica senza password e parla solo col sito")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
