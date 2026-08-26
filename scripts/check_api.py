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

import hashlib
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from nivult import auth  # noqa: E402
from nivult.api import app as app_module  # noqa: E402
from nivult.api.app import create_app  # noqa: E402
from nivult.config import database_name, database_url, safe_dsn  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def seen_by_other(sql: str, params=()):
    """Legge da una connessione in autocommit: passa solo ciò che è committato."""
    with psycopg.connect(database_url(), autocommit=True) as c:
        with c.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]


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
            # clusters non dipende da users: il CASCADE non lo tocca, e il
            # cluster di prova sopravviveva facendo crashare la prima
            # INSERT di qualunque suite girasse dopo questa.
            cur.execute("TRUNCATE users, clusters CASCADE")
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
                    cur.execute("TRUNCATE users, clusters CASCADE")
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

            print("\n— il primo next_digest_at —")
            # Il difetto che rendeva ogni iscritto inerte: lo slot lo scriveva
            # solo il worker DOPO un invio, quindi restava NULL e la query
            # degli utenti dovuti (next_digest_at IS NOT NULL) non lo vedeva
            # mai. Chi si iscriveva non riceveva un digest per sempre.
            check("appena iscritto non c'e' nessuno slot",
                  seen_by_other("SELECT next_digest_at FROM users "
                                "WHERE email = 'pref@test.dev'"), None)
            r = client.put("/me", headers={"Authorization": f"Bearer {sessione}"},
                           json={"frequency": "daily", "send_hour_local": 7,
                                 "send_weekday": None, "send_monthday": None})
            check("salvare l'orario lo calcola", r.status_code, 200)
            check("e adesso l'utente e' dovuto",
                  seen_by_other("SELECT next_digest_at IS NOT NULL FROM users "
                                "WHERE email = 'pref@test.dev'"), True)
            check("l'API lo restituisce al pannello",
                  bool(r.json().get("prossimo_digest")), True)
            # Spostare l'ora deve spostare lo slot, o sarebbe lo stesso bug
            # al contrario: orario nuovo, consegna al vecchio.
            primo = seen_by_other("SELECT next_digest_at FROM users "
                                  "WHERE email = 'pref@test.dev'")
            client.put("/me", headers={"Authorization": f"Bearer {sessione}"},
                       json={"send_hour_local": 19})
            check("e cambiarla lo ricalcola",
                  seen_by_other("SELECT next_digest_at FROM users "
                                "WHERE email = 'pref@test.dev'") != primo, True)

            r = client.put("/me", headers={"Authorization": f"Bearer {sessione}"},
                           json={"display_name": "Giuseppe Ranno"})
            check("il nome si puo' scrivere", r.json().get("nome"), "Giuseppe Ranno")

            print("\n— il primo digest non aspetta lo slot —")
            # Chi finisce l'iscrizione ha appena consegnato il proprio CV: è
            # il momento in cui va mostrato che il motore funziona. Con lo
            # slot normale un mensile avrebbe aspettato fino a un mese.
            # Vale solo se il digest può nascere davvero — CV attivo e una
            # ricerca attiva — altrimenti si programmerebbe un fallimento.
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("SELECT id FROM users WHERE email = 'pref@test.dev'")
                    uid = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO user_cvs (user_id, status, storage_key, "
                        "  encryption_algo, encrypted_dek, nonce, auth_tag, "
                        "  kek_version) "
                        # Le lunghezze le impone user_cvs_encryption_ck: DEK
                        # >= 32 byte, nonce 12, tag 16. Byte finti, misure vere.
                        "VALUES (%s,'active','k/primo','aes-256-gcm', "
                        "        decode(repeat('00', 40), 'hex'), "
                        "        decode(repeat('00', 12), 'hex'), "
                        "        decode(repeat('00', 16), 'hex'), 1)", (uid,))
                    cur.execute("INSERT INTO user_clusters (user_id, cluster_id) "
                                "VALUES (%s, %s)", (uid, cluster_id))
                    # Si torna alla condizione di chi non ha ancora ricevuto
                    # niente: è quella che decide, non l'ordine delle chiamate.
                    cur.execute("UPDATE users SET next_digest_at = NULL, "
                                "last_digest_at = NULL WHERE id = %s", (uid,))
                setup.commit()

            client.put("/me", headers={"Authorization": f"Bearer {sessione}"},
                       json={"frequency": "monthly", "send_monthday": 1,
                             "send_weekday": None})
            fra = seen_by_other(
                "SELECT next_digest_at - now() < interval '2 minutes' "
                "FROM users WHERE email = 'pref@test.dev'")
            check("profilo completo: il primo digest e' adesso", fra, True)

            # Dal secondo in poi comanda la frequenza scelta: se restasse
            # "adesso" ogni salvataggio farebbe ripartire un digest.
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE users SET last_digest_at = now() "
                                "WHERE id = %s", (uid,))
                setup.commit()
            client.put("/me", headers={"Authorization": f"Bearer {sessione}"},
                       json={"send_monthday": 15})
            check("gia' servito: si torna allo slot, non ad adesso",
                  seen_by_other("SELECT next_digest_at - now() > interval '1 day' "
                                "FROM users WHERE email = 'pref@test.dev'"), True)

            # Si rimette com'era: le prove che seguono lavorano sullo stesso
            # utente, e un mensile con send_monthday addosso fa rifiutare il
            # loro primo `frequency: daily`. Un test che sporca lo stato
            # rompe il vicino, e il guasto sembra del vicino.
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("DELETE FROM user_clusters WHERE user_id = %s", (uid,))
                    cur.execute("DELETE FROM user_cvs WHERE user_id = %s", (uid,))
                    cur.execute("UPDATE users SET frequency = 'daily', "
                                "  send_hour_local = 19, send_weekday = NULL, "
                                "  send_monthday = NULL, last_digest_at = NULL "
                                "WHERE id = %s", (uid,))
                setup.commit()


            print("\n— le offerte del pannello —")
            r = client.get("/me/offerte",
                           headers={"Authorization": f"Bearer {sessione}"})
            check("le offerte si leggono", r.status_code, 200)
            check("senza match l'elenco e' vuoto, non un errore",
                  r.json()["offerte"], [])
            check("e senza sessione non si leggono",
                  client.get("/me/offerte").status_code, 401)

            # Il logo: rotta pubblica di proposito, perche' deve funzionare
            # anche dentro un'email, dove non c'e' nessuna sessione.
            r = client.get("/logo/azienda-che-non-esiste")
            check("un logo introvabile e' 404, non un errore del server",
                  r.status_code, 404)
            check("e il fallimento resta scritto, o si riproverebbe sempre",
                  seen_by_other("SELECT count(*) FROM company_logos "
                                "WHERE chiave = 'azienda-che-non-esiste'"), 1)

            print("\n— aprire una ricerca —")
            r = client.post("/me/ricerca", headers={"Authorization": f"Bearer {sessione}"},
                            json={"family": "Non Esiste", "country": "it"})
            check("una famiglia fuori tassonomia e' rifiutata", r.status_code, 422)

            with psycopg.connect(database_url(), autocommit=True) as cc:
                cc.execute("UPDATE clusters SET last_successful_fetch_at = now() "
                           "WHERE family='Human Resources' AND country='IT'")
            r = client.post("/me/ricerca", headers={"Authorization": f"Bearer {sessione}"},
                            json={"family": "Human Resources", "country": "it"})
            check("una ricerca su un cluster esistente si apre", r.status_code, 201)
            check("un mercato gia' letto non risulta nuovo", r.json()["nuovo"], False)

            # Il caso che conta: una famiglia x paese che NON esiste ancora.
            # Il cluster va creato, o l'utente vede solo il nostro stato interno.
            r = client.post("/me/ricerca", headers={"Authorization": f"Bearer {sessione}"},
                            json={"family": "Human Resources", "country": "pt"})
            check("un mercato mai aperto viene creato", r.status_code, 201)
            check("e il sito sa che deve aspettare la prima ingestione",
                  r.json()["nuovo"], True)
            check("il cluster esiste davvero",
                  seen_by_other("SELECT count(*) FROM clusters "
                                "WHERE family='Human Resources' AND country='PT'"), 1)

            # Il tetto del piano e' anche il freno sui crediti: ogni ricerca
            # e' un cluster che consuma la quota della fonte finche' vive.
            r = client.post("/me/ricerca", headers={"Authorization": f"Bearer {sessione}"},
                            json={"family": "Human Resources", "country": "de"})
            check("oltre il tetto del piano si viene fermati", r.status_code, 409)
            # Ma cambiare i filtri di una ricerca che si ha gia' non consuma
            # una posizione: e' la stessa ricerca.
            r = client.post("/me/ricerca", headers={"Authorization": f"Bearer {sessione}"},
                            json={"family": "Human Resources", "country": "it",
                                  "filtri": {"languages": ["Italian"],
                                             "target_role": "HR Business Partner",
                                             "industries": ["Software Development"]}})
            check("ma si possono ancora cambiare i filtri di una che si ha",
                  r.status_code, 201)
            # Il ruolo e' la risposta alla domanda "a cosa ambisce questa
            # persona": deve sopravvivere al giro e tornare al pannello.
            check("il settore scelto viene salvato",
                  seen_by_other("SELECT industries FROM user_clusters uc "
                                "JOIN clusters c ON c.id = uc.cluster_id "
                                "WHERE c.country = 'IT' LIMIT 1"),
                  ["Software Development"])
            check("il ruolo a cui punta viene salvato",
                  seen_by_other("SELECT target_role FROM user_clusters uc "
                                "JOIN clusters c ON c.id = uc.cluster_id "
                                "WHERE c.country = 'IT' LIMIT 1"),
                  "HR Business Partner")

            print("\n— la famiglia si ricava dal ruolo —")
            # Il classificatore vero parla con GLM: nei test si sostituisce,
            # come _analizza_cv. Cio' che si prova e' il giro nostro — cache,
            # validazione, risposta — non il modello.
            app_module._classifica_ruolo = lambda fam, ruolo: "Human Resources"
            r = client.get("/ricerca/famiglia", params={"ruolo": "People Partner"},
                           headers={"Authorization": f"Bearer {sessione}"})
            check("il ruolo si classifica", r.json().get("famiglia"), "Human Resources")
            check("e la classificazione finisce in cache",
                  seen_by_other("SELECT family FROM role_family_cache "
                                "WHERE role_norm = 'people partner'"),
                  "Human Resources")
            app_module._classifica_ruolo = lambda fam, ruolo: (_ for _ in ()).throw(
                AssertionError("la cache doveva rispondere lei"))
            r = client.get("/ricerca/famiglia", params={"ruolo": "  People   PARTNER "},
                           headers={"Authorization": f"Bearer {sessione}"})
            check("la seconda volta risponde la cache, normalizzando",
                  r.json().get("famiglia"), "Human Resources")

            # Apertura senza famiglia: la ricava dal ruolo.
            r = client.post("/me/ricerca", headers={"Authorization": f"Bearer {sessione}"},
                            json={"country": "it",
                                  "filtri": {"target_role": "People Partner"}})
            check("si apre una ricerca dando solo ruolo e paese", r.status_code, 201)
            check("e la risposta dice su quale scaffale e' caduta",
                  r.json().get("famiglia"), "Human Resources")
            r = client.post("/me/ricerca", headers={"Authorization": f"Bearer {sessione}"},
                            json={"country": "it", "filtri": {}})
            check("senza ruolo ne' famiglia si viene fermati", r.status_code, 422)


            with psycopg.connect(database_url(), autocommit=True) as cc:
                cc.execute("DELETE FROM user_clusters uc USING clusters c "
                           "WHERE c.id = uc.cluster_id AND c.country IN ('PT','IT')")

            r = client.get("/vocabolari")
            check("i vocabolari ci sono", r.status_code, 200)
            v = r.json()
            lingue = {l["codice"] for l in v["lingue"]}
            check("le lingue vengono dal vocabolario in tabella",
                  {"Italian", "French"} <= lingue, True)
            # Ogni voce porta la sua etichetta leggibile: senza, all'utente
            # arriverebbero le parole del database — `FULL_TIME` e
            # `staffing_agency` erano esattamente questo.
            check("ogni vocabolario porta l'etichetta, non solo il codice",
                  all(isinstance(x, dict) and x.get("etichetta")
                      for chiave in ("lingue", "tipi_contratto", "tipi_datore",
                                     "modalita_lavoro", "livelli_esperienza")
                      for x in v[chiave]), True)
            check("nessuna etichetta e' rimasta uguale al codice grezzo",
                  [x["codice"] for x in v["tipi_contratto"]
                   if x["etichetta"] == x["codice"]], [])
            check("la sponsorship del visto arriva al sito",
                  len(v["sponsorship_visto"]) >= 2, True)
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

            # --- CV: cifratura, storage, profilo ---------------------------------
            os.environ["CV_KEK"] = secrets.token_hex(32)
            salvati: dict[str, bytes] = {}
            salva_originale = app_module.storage.salva
            elimina_originale = app_module.storage.elimina
            analizza_originale = app_module._analizza_cv
            app_module.storage.salva = lambda chiave, dati: salvati.__setitem__(chiave, dati)
            app_module.storage.elimina = lambda chiave: salvati.pop(chiave, None)
            app_module._analizza_cv = lambda conn, testo: {
                "families": ["Human Resources"], "seniority": "5-10",
                "skills": ["recruiting", "employee relations"],
                "languages": ["Italian", "English"], "years_experience": 8,
                "raw_extraction": {"note": "finto"},
            }

            try:
                contenuto = b"Curriculum di prova: HR Business Partner con 8 anni."
                r = client.post("/me/cv", files={"file": ("cv.txt", contenuto, "text/plain")},
                                headers=auth_header)
                check("upload del CV: 200 col profilo proposto", r.status_code, 200)
                profilo = r.json().get("profilo", {})
                check("il profilo arriva per la precompilazione",
                      (profilo.get("families"), profilo.get("seniority")),
                      (["Human Resources"], "5-10"))
                check("GET /me/cv rilegge il profilo del CV attivo",
                      client.get("/me/cv", headers=auth_header).json().get("skills"),
                      ["recruiting", "employee relations"])
                check("nello storage è finito ESATTAMENTE un oggetto",
                      len(salvati), 1)
                chiave_salvata, byte_salvati = next(iter(salvati.items()))
                check("lo storage non ha ricevuto il testo in chiaro",
                      contenuto in byte_salvati, False)
                check("la chiave dell'oggetto è sotto cv/<utente>/",
                      chiave_salvata.startswith("cv/"), True)
                check("lo schema della 0009 è rispettato nella riga",
                      seen_by_other(
                          "SELECT length(encrypted_dek) || '/' || length(nonce) || "
                          "  '/' || length(auth_tag) FROM user_cvs WHERE user_id = ("
                          "  SELECT id FROM users WHERE email = 'pref@test.dev') "
                          "  AND status = 'active'"),
                      "60/12/16")
                check("lo sha256 del file originale è registrato",
                      seen_by_other(
                          "SELECT sha256 FROM user_cvs WHERE user_id = ("
                          "  SELECT id FROM users WHERE email = 'pref@test.dev') "
                          "  AND status = 'active'"),
                      hashlib.sha256(contenuto).hexdigest())
                check("i valori fuori vocabolario di GLM non passano: languages salvate",
                      seen_by_other("SELECT languages::text FROM user_cvs WHERE user_id = ("
                                    "  SELECT id FROM users WHERE email = 'pref@test.dev') "
                                    "  AND status = 'active'"),
                      '["Italian", "English"]')

                secondo = b"Secondo curriculum, aggiornato: Chief People Officer."
                r = client.post("/me/cv", files={"file": ("cv2.txt", secondo, "text/plain")},
                                headers=auth_header)
                check("il secondo upload va a buon fine", r.status_code, 200)
                check("il primo CV è degradato a superseded: uno attivo solo",
                      seen_by_other("SELECT count(*) FROM user_cvs WHERE user_id = ("
                                    "  SELECT id FROM users WHERE email = 'pref@test.dev') "
                                    "  AND status = 'active'"), 1)
                check("il vecchio file cifrato è stato eliminato dal bucket",
                      len(salvati), 1)

                r = client.post("/me/cv", files={"file": ("cv.txt", b" corto ", "text/plain")},
                                headers=auth_header)
                check("un file senza testo sufficiente è rifiutato", r.status_code, 422)
            finally:
                app_module.storage.salva = salva_originale
                app_module.storage.elimina = elimina_originale
                app_module._analizza_cv = analizza_originale
                del os.environ["CV_KEK"]

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
            # clusters non dipende da users: il CASCADE non lo tocca, e il
            # cluster di prova sopravviveva facendo crashare la prima
            # INSERT di qualunque suite girasse dopo questa.
            cur.execute("TRUNCATE users, clusters CASCADE")
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
