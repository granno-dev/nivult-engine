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
import io
import os
import secrets
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from docx import Document  # noqa: E402

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
    basi: list[str | None] = []
    contenuti_email: list[tuple[str, str, str]] = []
    originale = auth.richiedi_magic_link

    def intercetta(conn, email, **kw):
        basi.append(kw.get("base"))
        kw["invia"] = lambda destinatario, oggetto, testo, html: \
            contenuti_email.append((oggetto, testo, html))
        tokens.append(originale(conn, email, **kw))
        return tokens[-1]

    auth.richiedi_magic_link = intercetta
    os.environ.pop("GLM_API_KEY", None)
    os.environ["ATS_DATABASE_URL"] = database_url()
    importa_originale = app_module.ponte_ats.importa
    app_module.ponte_ats.importa = lambda *args, **kwargs: app_module.ponte_ats.Riepilogo()
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

            r = client.post("/auth/magic-link", json={
                "email": "redirect-security@test.dev",
                "redirect": "https://localhost.attacker.example/verify"})
            check("redirect malevolo: risposta esterna invariata", r.status_code, 202)
            check("redirect malevolo NON viene usato", basi[-1], None)
            r = client.post("/auth/magic-link", json={
                "email": "redirect-local@test.dev",
                "redirect": "http://localhost:3000/verify"})
            check("localhost esatto resta ammesso per lo sviluppo",
                  (r.status_code, basi[-1]), (202, "http://localhost:3000"))

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

            os.environ["MICROSOFT_CLIENT_ID"] = "api-check-client"
            os.environ["MICROSOFT_CLIENT_SECRET"] = "api-check-secret"
            r = client.post("/me/oauth/microsoft/collega",
                            headers={"Authorization": f"Bearer {sessione}"})
            check("collegamento Microsoft autenticato: URL provider",
                  (r.status_code, "login.microsoftonline.com" in r.json().get("url", "")),
                  (200, True))
            check("lo state appartiene all'utente della sessione, non al client",
                  seen_by_other("SELECT count(*) FROM oauth_flows WHERE link_user_id = ("
                                "SELECT id FROM users WHERE email='api@test.dev')"), 1)
            del os.environ["MICROSOFT_CLIENT_ID"]
            del os.environ["MICROSOFT_CLIENT_SECRET"]

            auth_header = {"Authorization": f"Bearer {sessione}"}
            r = client.get("/me/sicurezza")
            check("sicurezza senza sessione: 401", r.status_code, 401)
            r = client.get("/me/sicurezza", headers=auth_header)
            check("sicurezza: sessione corrente riconosciuta",
                  (r.status_code, len(r.json()["sessioni"]),
                   sum(s["corrente"] for s in r.json()["sessioni"]),
                   "token_hash" in r.text), (200, 1, 1, False))

            client.get("/me/chiavi", headers=auth_header)
            r = client.get("/me/chiavi", headers=auth_header)
            check("il primo accesso mostra 1.000 crediti, non zero",
                  r.json()["saldo"]["balance"], 1000)
            check("onboarding prima della prima chiamata",
                  r.json()["prima_chiamata_fatta"], False)
            chiave_id = r.json()["chiavi"][0]["id"]
            from nivult.api_clienti import chiavi as api_chiavi
            api_chiavi.addebita_chiave(chiave_id, via="api", azione="/v1/jobs")
            r = client.get("/me/chiavi", headers=auth_header)
            check("onboarding dopo la prima chiamata",
                  r.json()["prima_chiamata_fatta"], True)
            check("chiave espone ultimo uso reale",
                  bool(r.json()["chiavi"][0]["ultimo_uso"]), True)
            r = client.post("/me/chiavi", headers=auth_header,
                            json={"etichetta": "Laptop <script>"})
            check("creazione chiave con etichetta personalizzata",
                  (r.status_code, len(r.json()["chiave"]) > 30), (200, True))

            from nivult.api_clienti import dati as api_dati
            stima_originale = api_dati.stima_export
            api_dati.stima_export = lambda filtri: 0
            try:
                r = client.post("/portale/export/stima", headers=auth_header,
                                json={"country": "AS", "technology": "SAP"})
            finally:
                api_dati.stima_export = stima_originale
            check("export da zero righe costa zero e non viene confuso con un errore",
                  (r.status_code, r.json()["righe"], r.json()["crediti"]), (200, 0, 0))

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("SELECT id::text FROM users WHERE email='api@test.dev'")
                    owner_id = cur.fetchone()[0]
                    altro_token = secrets.token_urlsafe(32)
                    cur.execute("INSERT INTO sessions (user_id, token_hash, expires_at, origin) "
                                "VALUES (%s,%s,now()+interval '1 day','magic_link') "
                                "RETURNING id::text",
                                (owner_id, hashlib.sha256(altro_token.encode()).hexdigest()))
                    altra_sessione = cur.fetchone()[0]
                    cur.execute("INSERT INTO users (email, plan, subscription_status, frequency, send_weekday, delivery_channels) "
                                "VALUES ('sessioni-altro@test.dev','basic','active','weekly',1,'{email}') "
                                "RETURNING id::text")
                    altro_uid = cur.fetchone()[0]
                    cur.execute("INSERT INTO sessions (user_id, token_hash, expires_at, origin) "
                                "VALUES (%s,%s,now()+interval '1 day','magic_link') "
                                "RETURNING id::text", (altro_uid, hashlib.sha256(b"altro").hexdigest()))
                    sessione_altro = cur.fetchone()[0]
                setup.commit()
            r = client.get("/me/sicurezza", headers=auth_header)
            check("sicurezza mostra anche la seconda sessione",
                  len(r.json()["sessioni"]), 2)
            r = client.delete(f"/me/sessioni/{sessione_altro}", headers=auth_header)
            check("non si revoca una sessione di un altro utente", r.status_code, 404)
            r = client.delete("/me/sessioni/non-un-uuid", headers=auth_header)
            check("ID sessione malformato: 404, non 500", r.status_code, 404)
            r = client.post("/me/sessioni/revoca-altre", headers=auth_header)
            check("revoca delle altre sessioni: una sola", r.json()["revocate"], 1)
            check("la sessione corrente resta attiva",
                  client.get("/me", headers=auth_header).status_code, 200)
            check("l'altra sessione e' revocata",
                  seen_by_other("SELECT revoked_at IS NOT NULL FROM sessions WHERE id=%s",
                                (altra_sessione,)), True)
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("INSERT INTO sessions (user_id, token_hash, expires_at, origin) "
                                "VALUES (%s,%s,now()+interval '1 day','magic_link') "
                                "RETURNING id::text",
                                (owner_id, hashlib.sha256(b"da-revocare").hexdigest()))
                    altra_sessione = cur.fetchone()[0]
                setup.commit()
            r = client.delete(f"/me/sessioni/{altra_sessione}", headers=auth_header)
            check("revoca singola della propria sessione", r.status_code, 200)
            check("revoca singola confermata nel database",
                  seen_by_other("SELECT revoked_at IS NOT NULL FROM sessions WHERE id=%s",
                                (altra_sessione,)), True)

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
            check("senza una ricerca ereditata attiva il pannello non mostra uno slot",
                  r.json().get("prossimo_digest"), None)
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
            # Giorno e ora locali, non "oltre un giorno": anche uno slot
            # mensile corretto può essere a meno di 24 ore da adesso.
            check("gia' servito: si torna allo slot, non ad adesso",
                  seen_by_other(
                      "SELECT next_digest_at > now() "
                      "AND EXTRACT(day FROM next_digest_at AT TIME ZONE timezone) "
                      "  = send_monthday "
                      "AND next_digest_at AT TIME ZONE timezone = "
                      "  date_trunc('day', next_digest_at AT TIME ZONE timezone) "
                      "  + send_hour_local * interval '1 hour' "
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

            print("\n— onboarding in ordine diverso —")
            client.post("/auth/magic-link", json={"email": "order@test.dev"})
            r = client.post("/auth/consuma", json={"token": tokens[-1]})
            ordine_header = {"Authorization": f"Bearer {r.json()['sessione']}"}
            client.put("/me", headers=ordine_header,
                       json={"frequency": "monthly", "send_monthday": 28,
                             "send_hour_local": 8})
            prima = seen_by_other(
                "SELECT next_digest_at FROM users WHERE email='order@test.dev'")
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("SELECT id FROM users WHERE email='order@test.dev'")
                    ordine_uid = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO user_cvs (user_id,status,storage_key,encryption_algo,"
                        "  encrypted_dek,nonce,auth_tag,kek_version) "
                        "VALUES (%s,'active','cv/order','aes-256-gcm',"
                        "  decode(repeat('00',40),'hex'),decode(repeat('00',12),'hex'),"
                        "  decode(repeat('00',16),'hex'),1)", (ordine_uid,))
                setup.commit()
            r = client.put(f"/me/cluster/{cluster_id}", json={}, headers=ordine_header)
            check("la ricerca completata dopo CV e preferenze e' accettata",
                  r.status_code, 204)
            check("e il primo digest diventa immediato senza un altro PUT",
                  seen_by_other("SELECT next_digest_at < now() + interval '2 minutes' "
                                "FROM users WHERE email='order@test.dev'"), True)
            check("lo slot mensile precedente e' stato anticipato",
                  seen_by_other("SELECT next_digest_at < %s FROM users "
                                "WHERE email='order@test.dev'", (prima,)), True)

            r = client.post("/me/ricerca", headers=ordine_header,
                            json={"family": "Human Resources", "country": "IT",
                                  "filtri": {"target_role": "Ruolo base"}})
            check("la ricerca generica esistente riceve il primo ruolo",
                  r.status_code, 201)
            globale_ordine = seen_by_other(
                "SELECT next_digest_at FROM users WHERE email='order@test.dev'")
            r = client.post("/me/ricerca", headers=ordine_header,
                            json={"family": "Human Resources", "country": "IT",
                                  "filtri": {"target_role": "Primo override",
                                             "frequency": "daily", "send_hour_local": 6,
                                             "timezone": "Europe/Rome"}})
            check("nuovo override con CV e nessun digest: primo slot immediato",
                  (r.status_code,
                   datetime.fromisoformat(r.json()["next_digest_at"])
                   <= datetime.now(timezone.utc) + timedelta(minutes=2)),
                  (201, True))
            override_ordine = r.json()["id"]
            check("l'anticipo specifico non riscrive la scadenza globale",
                  seen_by_other("SELECT next_digest_at FROM users "
                                "WHERE email='order@test.dev'"), globale_ordine)
            r = client.put(f"/me/cluster/{override_ordine}", headers=ordine_header,
                           json={"target_role": "Primo override", "frequency": "monthly",
                                 "send_monthday": 20, "send_hour_local": 9,
                                 "timezone": "Europe/Rome"})
            check("modifica successiva della pianificazione accettata", r.status_code, 204)
            override_modificato = seen_by_other(
                "SELECT next_digest_at FROM user_clusters WHERE id = %s", (override_ordine,))
            check("modifica successiva non forza un altro invio immediato",
                  override_modificato > datetime.now(timezone.utc) + timedelta(minutes=2),
                  True)
            r = client.put("/me", headers=ordine_header, json={"send_hour_local": 18})
            check("cambio del default account riuscito", r.status_code, 200)
            check("cambio del default non tocca la pianificazione personalizzata",
                  seen_by_other("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                                (override_ordine,)), override_modificato)

            print("\n— collegamento Telegram —")
            # Il bot non puo' scrivere per primo: tutto il giro esiste per
            # ottenere un chat_id, e il webhook e' l'unico punto in cui
            # arriva. Qui si prova soprattutto che quella rotta pubblica non
            # si lasci comandare da chiunque.
            os.environ["TELEGRAM_BOT_TOKEN"] = "000:prova"
            os.environ["TELEGRAM_BOT_USERNAME"] = "NivultBot"
            os.environ["TELEGRAM_WEBHOOK_SECRET"] = "segreto-di-prova"
            inviati: list[tuple] = []
            tg_mod = app_module.telegram_mod
            vero_invia_testo = tg_mod.invia_testo
            tg_mod.invia_testo = lambda chat, testo: inviati.append((chat, testo)) or "1"

            r = client.post("/me/telegram/collega", headers=auth_header)
            check("apre un collegamento: 200", r.status_code, 200)
            link = r.json().get("link", "")
            check("il link punta al bot", link.startswith("https://t.me/NivultBot?start="), True)
            gettone = link.rsplit("=", 1)[-1]
            check("il gettone NON e' in chiaro nel database",
                  seen_by_other("SELECT count(*) FROM telegram_link_tokens "
                                "WHERE token_hash = %s", (gettone,)), 0)
            check("in tabella c'e' il suo sha256",
                  seen_by_other("SELECT count(*) FROM telegram_link_tokens "
                                "WHERE token_hash = %s",
                                (hashlib.sha256(gettone.encode()).hexdigest(),)), 1)
            check("prima di START non e' collegato",
                  client.get("/me/telegram/stato", headers=auth_header).json()["collegato"],
                  False)

            # IL PUNTO: senza il segreto quella rotta collegherebbe la chat di
            # chiunque all'account di chiunque.
            corpo = {"message": {"chat": {"id": 555001}, "text": f"/start {gettone}"}}
            check("webhook SENZA header segreto: rifiutato",
                  client.post("/telegram/webhook", json=corpo).status_code, 403)
            check("webhook con segreto SBAGLIATO: rifiutato",
                  client.post("/telegram/webhook", json=corpo,
                              headers={"X-Telegram-Bot-Api-Secret-Token": "quasi"}
                              ).status_code, 403)
            check("e la chat non e' stata collegata lo stesso",
                  seen_by_other("SELECT telegram_chat_id IS NULL FROM users "
                                "WHERE email = 'pref@test.dev'"), True)

            buono = {"X-Telegram-Bot-Api-Secret-Token": "segreto-di-prova"}
            check("webhook col segreto giusto: 200",
                  client.post("/telegram/webhook", json=corpo, headers=buono).status_code, 200)
            check("adesso la chat e' collegata",
                  seen_by_other("SELECT telegram_chat_id FROM users "
                                "WHERE email = 'pref@test.dev'"), "555001")
            check("e il bot ha salutato", len(inviati), 1)
            check("lo stato lo dice alla pagina",
                  client.get("/me/telegram/stato", headers=auth_header).json()["collegato"],
                  True)

            # Monouso, come il magic link: chi rigioca lo stesso START non
            # ricollega niente.
            inviati.clear()
            client.post("/telegram/webhook",
                        json={"message": {"chat": {"id": 555999},
                                          "text": f"/start {gettone}"}}, headers=buono)
            check("gettone gia' usato: la chat NON cambia",
                  seen_by_other("SELECT telegram_chat_id FROM users "
                                "WHERE email = 'pref@test.dev'"), "555001")

            # Una chat appartiene a un utente solo, o il secondo riceverebbe i
            # digest del primo senza che nessuno se ne accorga.
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("INSERT INTO users (email, plan, frequency, "
                                "  delivery_channels, subscription_status) "
                                "VALUES ('tg2@test.dev','basic','daily','{email}',"
                                "        'trialing') RETURNING id")
                    altro = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO telegram_link_tokens (user_id, token_hash, expires_at) "
                        "VALUES (%s, %s, now() + interval '10 minutes')",
                        (altro, hashlib.sha256(b"secondo").hexdigest()))
                setup.commit()
            inviati.clear()
            client.post("/telegram/webhook",
                        json={"message": {"chat": {"id": 555001},
                                          "text": "/start secondo"}}, headers=buono)
            check("una chat gia' presa non si ruba",
                  seen_by_other("SELECT telegram_chat_id IS NULL FROM users "
                                "WHERE email = 'tg2@test.dev'"), True)
            check("e all'utente viene spiegato perche'", len(inviati), 1)

            # Scollegando, il canale non puo' restare 'telegram': sarebbe un
            # utente che non riceve piu' niente senza saperlo.
            client.put("/me", headers=auth_header,
                       json={"delivery_channels": ["telegram"]})
            check("scollega: 204",
                  client.delete("/me/telegram", headers=auth_header).status_code, 204)
            check("e il canale torna a email",
                  seen_by_other("SELECT delivery_channels FROM users "
                                "WHERE email = 'pref@test.dev'"), ["email"])

            tg_mod.invia_testo = vero_invia_testo
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("DELETE FROM users WHERE email = 'tg2@test.dev'")
                setup.commit()

            print("\n— collegamento WhatsApp —")
            # Qui la prova di possesso e' il punto: se bastasse digitare un
            # numero, chiunque potrebbe inserire quello di un altro e
            # fargli piovere addosso digest. Il numero entra SOLO dal
            # messaggio che l'utente ci manda, mai dal PUT.
            # 04/10/2026: il trasporto e' wuzapi self-hosted (morto Zernio) —
            # i nomi delle variabili cambiano, il contratto no.
            os.environ["WUZAPI_TOKEN"] = "tok_prova"
            os.environ["WUZAPI_NUMBER"] = "16469143141"
            wa_mod = app_module.whatsapp_mod
            vero_cerca = wa_mod.cerca_collegamenti
            vero_wa_testo = wa_mod.invia_testo
            wa_inviati: list[tuple] = []
            wa_mod.invia_testo = lambda conv, testo: wa_inviati.append((conv, testo)) or "1"

            check("scegliere whatsapp SENZA collegamento: 422",
                  client.put("/me", headers=auth_header,
                             json={"delivery_channels": ["whatsapp"]}).status_code, 422)
            check("e il numero NON si scrive dal PUT",
                  client.put("/me", headers=auth_header,
                             json={"delivery_channels": ["whatsapp", "email"],
                                   "whatsapp_e164": "+390000000001"}).status_code, 422)

            r = client.post("/me/whatsapp/collega", headers=auth_header)
            check("apre un collegamento: 200", r.status_code, 200)
            wa_link = r.json().get("link", "")
            check("il link e' wa.me col testo precompilato",
                  wa_link.startswith("https://wa.me/16469143141?text=NIVULT%20"), True)
            wa_gettone = wa_link.rsplit("NIVULT%20", 1)[-1]
            check("il gettone non e' in chiaro nel database",
                  seen_by_other("SELECT count(*) FROM whatsapp_link_tokens "
                                "WHERE token_hash = %s", (wa_gettone,)), 0)

            # Nessun messaggio arrivato: non collegato.
            wa_mod.cerca_collegamenti = lambda: []
            check("inbox vuota: non collegato",
                  client.get("/me/whatsapp/stato", headers=auth_header)
                  .json()["collegato"], False)

            # Un gettone INVENTATO nell'inbox non collega niente: l'hash fa
            # da giudice.
            wa_mod.cerca_collegamenti = lambda: [{
                "gettone_hash": hashlib.sha256(b"inventato").hexdigest(),
                "telefono": "+390000000009", "conversazione": "conv_x"}]
            check("un gettone inventato non collega",
                  client.get("/me/whatsapp/stato", headers=auth_header)
                  .json()["collegato"], False)

            # Il messaggio giusto: il gettone vero, dal telefono dell'utente.
            wa_mod.cerca_collegamenti = lambda: [{
                "gettone_hash": hashlib.sha256(wa_gettone.encode()).hexdigest(),
                "telefono": "+393926045981", "conversazione": "conv_1"}]
            check("il messaggio giusto collega",
                  client.get("/me/whatsapp/stato", headers=auth_header)
                  .json()["collegato"], True)
            check("il numero e' quello del mittente",
                  seen_by_other("SELECT whatsapp_e164 FROM users "
                                "WHERE email = 'pref@test.dev'"), "+393926045981")
            check("la conversazione e' registrata",
                  seen_by_other("SELECT whatsapp_conversation_id FROM users "
                                "WHERE email = 'pref@test.dev'"), "conv_1")
            check("e la conferma e' partita (gratis, finestra aperta)",
                  len(wa_inviati), 1)
            check("adesso il canale whatsapp si puo' scegliere",
                  client.put("/me", headers=auth_header,
                             json={"delivery_channels": ["email", "whatsapp"]}).status_code, 200)

            # Monouso: lo stesso gettone rigiocato non riassegna niente.
            wa2 = seen_by_other("SELECT count(*) FROM whatsapp_link_tokens "
                                "WHERE consumed_at IS NOT NULL AND phone_e164 = "
                                "'+393926045981'")
            client.get("/me/whatsapp/stato", headers=auth_header)
            check("il gettone e' monouso", seen_by_other(
                "SELECT count(*) FROM whatsapp_link_tokens "
                "WHERE consumed_at IS NOT NULL AND phone_e164 = "
                "'+393926045981'"), wa2)

            # Un numero gia' collegato non si ruba da un secondo account.
            # Serve la SESSIONE del secondo account: la scansione dell'inbox
            # parte solo quando chi interroga ha un gettone aperto — il
            # lavoro si fa solo mentre qualcuno sta davvero collegando.
            client.post("/auth/magic-link", json={"email": "wa2@test.dev"})
            r = client.post("/auth/consuma", json={"token": tokens[-1]})
            wa2_header = {"Authorization": f"Bearer {r.json()['sessione']}"}
            r = client.post("/me/whatsapp/collega", headers=wa2_header)
            wa2_gettone = r.json()["link"].rsplit("NIVULT%20", 1)[-1]
            wa_inviati.clear()
            wa_mod.cerca_collegamenti = lambda: [{
                "gettone_hash": hashlib.sha256(wa2_gettone.encode()).hexdigest(),
                "telefono": "+393926045981", "conversazione": "conv_2"}]
            check("un numero gia' preso non si ruba",
                  client.get("/me/whatsapp/stato", headers=wa2_header)
                  .json()["collegato"], False)
            check("il numero resta al primo account",
                  seen_by_other("SELECT whatsapp_e164 IS NULL FROM users "
                                "WHERE email = 'wa2@test.dev'"), True)
            check("e a chi ci prova viene spiegato", len(wa_inviati), 1)

            check("scollega: 204",
                  client.delete("/me/whatsapp", headers=auth_header).status_code, 204)
            check("e il canale torna a email",
                  seen_by_other("SELECT delivery_channels FROM users "
                                "WHERE email = 'pref@test.dev'"), ["email"])

            wa_mod.cerca_collegamenti = vero_cerca
            wa_mod.invia_testo = vero_wa_testo
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("DELETE FROM users WHERE email = 'wa2@test.dev'")
                setup.commit()

            print("\n— le offerte del pannello —")
            r = client.get("/me/offerte",
                           headers={"Authorization": f"Bearer {sessione}"})
            check("le offerte si leggono", r.status_code, 200)
            check("senza match l'elenco e' vuoto, non un errore",
                  r.json()["offerte"], [])
            check("e senza sessione non si leggono",
                  client.get("/me/offerte").status_code, 401)
            check("anche lo storico digest parte vuoto",
                  client.get("/me/digest", headers=auth_header).json(), [])

            # Il logo: rotta pubblica di proposito, perche' deve funzionare
            # anche dentro un'email, dove non c'e' nessuna sessione.
            r = client.get("/logo/azienda-che-non-esiste")
            check("un logo introvabile e' 404, non un errore del server",
                  r.status_code, 404)
            check("e il fallimento resta scritto, o si riproverebbe sempre",
                  seen_by_other("SELECT count(*) FROM company_logos "
                                "WHERE chiave = 'azienda-che-non-esiste'"), 1)
            import struct
            png_piccolo = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
                           + struct.pack(">II", 16, 16))
            png_grande = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
                          + struct.pack(">II", 100, 100))
            check("favicon troppo piccola scartata", app_module._logo_nitido(png_piccolo), False)
            check("logo 100x100 reale accettato", app_module._logo_nitido(png_grande), True)
            pagina_abb = (b'<meta property="og:image" content="https://media.licdn.com/dms/image/'
                          b'company-logo_100_100/abb_logo?e=2147483647&amp;v=beta&amp;t=abc">')
            originale_download = app_module._scarica_logo
            try:
                app_module._scarica_logo = lambda *args, **kwargs: pagina_abb
                check("logo aziendale da pagina pubblica LinkedIn",
                      app_module._logo_linkedin_pubblico("abb"),
                      "https://media.licdn.com/dms/image/company-logo_100_100/abb_logo?e=2147483647&v=beta&t=abc")
                check("logo aziendale anche da jobs-guest",
                      app_module._logo_linkedin_pubblico("abb", "4483738935"),
                      "https://media.licdn.com/dms/image/company-logo_100_100/abb_logo?e=2147483647&v=beta&t=abc")
                check("slug LinkedIn non valido rifiutato",
                      app_module._logo_linkedin_pubblico("../indeed"), None)
            finally:
                app_module._scarica_logo = originale_download

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
            with psycopg.connect(database_url()) as setup:
                cluster_de = setup.execute(
                    "INSERT INTO clusters (family,country) VALUES ('Human Resources','DE') "
                    "ON CONFLICT (family,country) DO UPDATE SET updated_at = now() "
                    "RETURNING id", ()).fetchone()
                if cluster_de is None:
                    cluster_de = setup.execute(
                        "SELECT id FROM clusters WHERE family='Human Resources' AND country='DE'"
                    ).fetchone()
                setup.commit()
            r = client.put(f"/me/cluster/{cluster_de[0]}", json={},
                           headers={"Authorization": f"Bearer {sessione}"})
            check("anche la PUT diretta non aggira il tetto del piano", r.status_code, 409)
            check("e la terza iscrizione non viene scritta",
                  seen_by_other("SELECT count(*) FROM user_clusters uc "
                                "JOIN clusters c ON c.id = uc.cluster_id "
                                "JOIN users u ON u.id = uc.user_id "
                                "WHERE u.email='pref@test.dev' AND c.country='DE'"), 0)
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
                                "JOIN users u ON u.id = uc.user_id "
                                "WHERE u.email = 'pref@test.dev' AND c.country = 'IT' LIMIT 1"),
                  ["Software Development"])
            check("il ruolo a cui punta viene salvato",
                  seen_by_other("SELECT target_role FROM user_clusters uc "
                                "JOIN clusters c ON c.id = uc.cluster_id "
                                "JOIN users u ON u.id = uc.user_id "
                                "WHERE u.email = 'pref@test.dev' AND c.country = 'IT' LIMIT 1"),
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

            # Liberiamo una posizione: il secondo ruolo distinto nel cluster
            # non deve essere confuso con una modifica del primo.
            with psycopg.connect(database_url(), autocommit=True) as cc:
                cc.execute("DELETE FROM user_clusters uc USING clusters c "
                           "WHERE c.id = uc.cluster_id AND c.country = 'PT'")

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

            filtri_avanzati = dict(filtri, min_salary_annual=40000,
                                   salary_currency="eur",
                                   desired_technologies=["Python", "React"])
            r = client.put(f"/me/cluster/{cluster_id}", json=filtri_avanzati,
                           headers=auth_header)
            check("salario minimo e tecnologie si salvano: 204", r.status_code, 204)
            r = client.get("/me/cluster", headers=auth_header)
            avanzati = r.json()[0]["filtri"]
            check("salario e tecnologie si rileggono normalizzati",
                  (avanzati["min_salary_annual"], avanzati["salary_currency"],
                   avanzati["desired_technologies"]),
                  (40000, "EUR", ["Python", "React"]))
            check("salario senza valuta è rifiutato",
                  client.put(f"/me/cluster/{cluster_id}",
                             json=dict(filtri_avanzati, salary_currency=None),
                             headers=auth_header).status_code, 422)
            check("oltre quindici tecnologie è rifiutato",
                  client.put(f"/me/cluster/{cluster_id}",
                             json=dict(filtri_avanzati,
                                       desired_technologies=[f"tech-{i}" for i in range(16)]),
                             headers=auth_header).status_code, 422)

            check("headcount negativo e' un 422, non un errore DB",
                  client.put(f"/me/cluster/{cluster_id}",
                             json=dict(filtri, min_headcount=-1),
                             headers=auth_header).status_code, 422)
            check("headcount massimo zero e' un 422",
                  client.put(f"/me/cluster/{cluster_id}",
                             json=dict(filtri, max_headcount=0),
                             headers=auth_header).status_code, 422)

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

            print("\n— frequenza per singola ricerca —")
            r = client.get("/me/cluster", headers=auth_header)
            ricerca_sched = r.json()[0]
            ricerca_sched_id = ricerca_sched["id"]
            check("ricerca legacy eredita tutto e non ha date specifiche",
                  (ricerca_sched["filtri"]["frequency"],
                   ricerca_sched["filtri"]["send_weekday"],
                   ricerca_sched["filtri"]["send_monthday"],
                   ricerca_sched["filtri"]["send_hour_local"],
                   ricerca_sched["filtri"]["timezone"],
                   ricerca_sched["next_digest_at"], ricerca_sched["last_digest_at"]),
                  (None, None, None, None, None, None, None))

            def put_sched(**scheduling):
                return client.put(f"/me/cluster/{ricerca_sched_id}",
                                  json=dict(filtri_avanzati, **scheduling),
                                  headers=auth_header)

            r = put_sched(frequency="weekly", send_weekday=3,
                          send_hour_local=9, timezone="Europe/Rome")
            check("override settimanale salvato", r.status_code, 204)
            check("giorno, ora e fuso specifici riletti dal database",
                  seen_by_other(
                      "SELECT frequency || ':' || send_weekday || ':' || send_hour_local "
                      "|| ':' || timezone || ':' || coalesce(send_monthday::text, 'NULL') "
                      "FROM user_clusters WHERE id = %s", (ricerca_sched_id,)),
                  "weekly:3:9:Europe/Rome:NULL")
            slot_weekly = seen_by_other(
                "SELECT next_digest_at FROM user_clusters WHERE id = %s",
                (ricerca_sched_id,))
            check("l'override ha una scadenza propria", slot_weekly is not None, True)

            r = put_sched()
            check("PUT senza campi scheduling resta 204", r.status_code, 204)
            check("assenza di scheduling conserva override e scadenza",
                  seen_by_other(
                      "SELECT frequency = 'weekly' AND send_weekday = 3 "
                      "AND next_digest_at = %s FROM user_clusters WHERE id = %s",
                      (slot_weekly, ricerca_sched_id)), True)
            r = put_sched(frequency="weekly", send_weekday=3,
                          send_hour_local=9, timezone="Europe/Rome")
            check("stessa pianificazione esplicita e' idempotente",
                  (r.status_code, seen_by_other(
                      "SELECT next_digest_at FROM user_clusters WHERE id = %s",
                      (ricerca_sched_id,))), (204, slot_weekly))

            slot_originale = app_module.calcola_slot
            chiamate_slot = []

            def osserva_slot(*args):
                chiamate_slot.append(args)
                return slot_originale(*args)

            app_module.calcola_slot = osserva_slot
            try:
                r = put_sched(frequency="monthly", send_monthday=12,
                              send_hour_local=6, timezone="America/New_York")
            finally:
                app_module.calcola_slot = slot_originale
            check("cambio a monthly senza residui weekly", r.status_code, 204)
            slot_monthly = seen_by_other(
                "SELECT next_digest_at FROM user_clusters WHERE id = %s",
                (ricerca_sched_id,))
            check("next_digest_at e' quello calcolato dal server con la nuova ora e fuso",
                  (len(chiamate_slot), slot_monthly),
                  (1, slot_originale(*chiamate_slot[-1])))
            check("weekday vecchio azzerato e monthday nuovo presente",
                  seen_by_other(
                      "SELECT send_weekday IS NULL AND send_monthday = 12 "
                      "AND send_hour_local = 6 AND timezone = 'America/New_York' "
                      "FROM user_clusters WHERE id = %s", (ricerca_sched_id,)), True)

            r = put_sched(frequency="daily", send_hour_local=7, timezone="Europe/Rome")
            check("cambio a daily senza residui dei giorni", r.status_code, 204)
            check("daily specifica ha entrambi i giorni NULL",
                  seen_by_other(
                      "SELECT frequency = 'daily' AND send_weekday IS NULL "
                      "AND send_monthday IS NULL FROM user_clusters WHERE id = %s",
                      (ricerca_sched_id,)), True)
            check("next_digest_at coerente anche dopo il secondo cambio",
                  seen_by_other(
                      "SELECT next_digest_at > now() AND "
                      "EXTRACT(hour FROM next_digest_at AT TIME ZONE 'Europe/Rome') = 7 "
                      "FROM user_clusters WHERE id = %s", (ricerca_sched_id,)), True)

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute(
                        "UPDATE users SET next_digest_at = now() + interval '20 days' "
                        "WHERE email = 'pref@test.dev'")
                    cur.execute(
                        "UPDATE user_clusters SET next_digest_at = now() + interval '2 days' "
                        "WHERE id = %s", (ricerca_sched_id,))
                    cur.execute("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                                (ricerca_sched_id,))
                    override_piu_vicino = cur.fetchone()[0]
                setup.commit()
            r = client.get("/me", headers=auth_header)
            check("/me espone il minimo fra globale e override senza riscrivere il globale",
                  (datetime.fromisoformat(r.json()["prossimo_digest"]),
                   seen_by_other(
                       "SELECT next_digest_at > now() + interval '19 days' FROM users "
                       "WHERE email = 'pref@test.dev'")),
                  (override_piu_vicino, True))

            ultimo_attivo = datetime(2026, 1, 1, tzinfo=timezone.utc)
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE user_clusters SET is_paused = true, "
                                "last_digest_at = %s WHERE id = %s",
                                (ultimo_attivo, ricerca_sched_id))
                setup.commit()
            r = client.get("/me", headers=auth_header)
            check("/me ignora la scadenza di una ricerca in pausa senza ereditate attive",
                  r.json()["prossimo_digest"], None)
            check("pausa conserva scadenza specifica e storico senza riscriverli",
                  seen_by_other(
                      "SELECT next_digest_at = %s AND last_digest_at = %s "
                      "FROM user_clusters WHERE id = %s",
                      (override_piu_vicino, ultimo_attivo, ricerca_sched_id)), True)
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE user_clusters SET is_paused = false WHERE id = %s",
                                (ricerca_sched_id,))
                    cur.execute("UPDATE clusters SET status = 'paused' WHERE id = %s",
                                (cluster_id,))
                setup.commit()
            r = client.get("/me", headers=auth_header)
            check("/me ignora la scadenza di un override con cluster inattivo",
                  r.json()["prossimo_digest"], None)
            check("cluster inattivo conserva next e last della ricerca",
                  seen_by_other(
                      "SELECT next_digest_at = %s AND last_digest_at = %s "
                      "FROM user_clusters WHERE id = %s",
                      (override_piu_vicino, ultimo_attivo, ricerca_sched_id)), True)
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE clusters SET status = 'active' WHERE id = %s",
                                (cluster_id,))
                setup.commit()
            r = client.get("/me", headers=auth_header)
            check("riattivare il cluster ripristina la scadenza esposta e non lo storico",
                  (datetime.fromisoformat(r.json()["prossimo_digest"]),
                   seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                                 (ricerca_sched_id,))),
                  (override_piu_vicino, ultimo_attivo))

            for etichetta, payload in (
                ("frequenza sconosciuta", {"frequency": "yearly"}),
                ("weekly senza giorno", {"frequency": "weekly"}),
                ("weekly con giorno nullo", {"frequency": "weekly", "send_weekday": None}),
                ("monthly senza giorno", {"frequency": "monthly"}),
                ("monthly con giorno nullo", {"frequency": "monthly", "send_monthday": None}),
                ("daily con weekday", {"frequency": "daily", "send_weekday": 1}),
                ("weekly con monthday", {"frequency": "weekly", "send_weekday": 1,
                                          "send_monthday": 1}),
                ("weekday fuori range", {"frequency": "weekly", "send_weekday": 8}),
                ("monthday fuori range", {"frequency": "monthly", "send_monthday": 29}),
                ("ora fuori range", {"frequency": "daily", "send_hour_local": 24}),
                ("fuso inesistente", {"frequency": "daily", "timezone": "Fuso/Fasullo"}),
                ("scheduling parziale senza frequenza", {"send_hour_local": 5}),
                ("next_digest_at non scrivibile", {"next_digest_at": "2030-01-01T00:00:00Z"}),
                ("last_digest_at non scrivibile", {"last_digest_at": "2020-01-01T00:00:00Z"}),
            ):
                check(f"422 per ricerca: {etichetta}", put_sched(**payload).status_code, 422)

            ultimo_specifico = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)
            globale_sentinella = datetime.now(timezone.utc) + timedelta(days=30)
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE user_clusters SET last_digest_at = %s WHERE id = %s",
                                (ultimo_specifico, ricerca_sched_id))
                    cur.execute("UPDATE users SET next_digest_at = %s "
                                "WHERE email = 'pref@test.dev'", (globale_sentinella,))
                setup.commit()
            r = put_sched(frequency=None)
            check("frequency null esplicito ripristina l'eredità", r.status_code, 204)
            check("reset cancella override e scadenza ma conserva lo storico personale",
                  seen_by_other(
                      "SELECT frequency IS NULL AND send_weekday IS NULL "
                      "AND send_monthday IS NULL AND send_hour_local IS NULL "
                      "AND timezone IS NULL AND next_digest_at IS NULL "
                      "AND last_digest_at = %s FROM user_clusters WHERE id = %s",
                      (ultimo_specifico, ricerca_sched_id)), True)
            check("reset dell'override non azzera la scadenza globale",
                  seen_by_other("SELECT next_digest_at FROM users "
                                "WHERE email = 'pref@test.dev'"), globale_sentinella)
            r = put_sched(frequency="daily", send_weekday=None, send_monthday=None,
                          send_hour_local=None, timezone=None)
            check("null espliciti su ora e fuso usano il fallback account", r.status_code, 204)
            check("fallback persistito come NULL, non come copia che diventa stale",
                  seen_by_other(
                      "SELECT send_hour_local IS NULL AND timezone IS NULL "
                      "AND next_digest_at IS NOT NULL FROM user_clusters WHERE id = %s",
                      (ricerca_sched_id,)), True)

            slot_reale = app_module.calcola_slot

            def put_account_osservato(payload):
                chiamate = []

                def osserva(*args):
                    chiamate.append(args)
                    return slot_reale(*args)

                app_module.calcola_slot = osserva
                try:
                    risposta = client.put("/me", headers=auth_header, json=payload)
                finally:
                    app_module.calcola_slot = slot_reale
                return risposta, chiamate

            r, chiamate_ora = put_account_osservato({"send_hour_local": 22})
            fuso_account = seen_by_other(
                "SELECT timezone FROM users WHERE email = 'pref@test.dev'")
            check("cambio ora account riuscito", r.status_code, 200)
            check("globale e override fallback usano lo stesso adesso e calcola_slot",
                  (len(chiamate_ora), len({args[-1] for args in chiamate_ora})), (2, 1))
            next_ora_atteso = slot_reale(
                "daily", 22, None, None, fuso_account, chiamate_ora[-1][-1])
            check("override con ora NULL ricalcolato sull'ora account, last invariato",
                  seen_by_other(
                      "SELECT next_digest_at = %s AND last_digest_at = %s "
                      "FROM user_clusters WHERE id = %s",
                      (next_ora_atteso, ultimo_specifico, ricerca_sched_id)), True)

            r, chiamate_fuso = put_account_osservato({"timezone": "America/New_York"})
            check("cambio fuso account riuscito", r.status_code, 200)
            check("anche il cambio fuso condivide lo stesso adesso",
                  (len(chiamate_fuso), len({args[-1] for args in chiamate_fuso})), (2, 1))
            next_fuso_atteso = slot_reale(
                "daily", 22, None, None, "America/New_York", chiamate_fuso[-1][-1])
            check("override con fuso NULL ricalcolato sul fuso account, last invariato",
                  seen_by_other(
                      "SELECT next_digest_at = %s AND last_digest_at = %s "
                      "FROM user_clusters WHERE id = %s",
                      (next_fuso_atteso, ultimo_specifico, ricerca_sched_id)), True)

            r = put_sched(frequency="weekly", send_weekday=2,
                          send_hour_local=5, timezone="Europe/Rome")
            check("imposto un override interamente esplicito", r.status_code, 204)
            next_esplicito = seen_by_other(
                "SELECT next_digest_at FROM user_clusters WHERE id = %s",
                (ricerca_sched_id,))
            r, chiamate_esplicito = put_account_osservato(
                {"send_hour_local": 1, "timezone": "UTC"})
            check("cambio congiunto ora e fuso account riuscito", r.status_code, 200)
            check("override esplicito: frequenza, giorno, ora, fuso e next invariati",
                  seen_by_other(
                      "SELECT frequency = 'weekly' AND send_weekday = 2 "
                      "AND send_hour_local = 5 AND timezone = 'Europe/Rome' "
                      "AND next_digest_at = %s AND last_digest_at = %s "
                      "FROM user_clusters WHERE id = %s",
                      (next_esplicito, ultimo_specifico, ricerca_sched_id)), True)
            check("override esplicito escluso dal ricalcolo fallback: solo slot globale",
                  len(chiamate_esplicito), 1)

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE users SET last_digest_at = %s "
                                "WHERE email = 'pref@test.dev'",
                                (ultimo_specifico - timedelta(days=1),))
                    cur.execute("UPDATE user_clusters SET is_paused = true WHERE id = %s",
                                (ricerca_sched_id,))
                setup.commit()
            r = client.get("/me", headers=auth_header)
            check("ultimo_digest e' il max storico anche con ricerca in pausa",
                  datetime.fromisoformat(r.json()["ultimo_digest"]), ultimo_specifico)
            ultimo_account = ultimo_specifico + timedelta(days=1)
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE users SET last_digest_at = %s "
                                "WHERE email = 'pref@test.dev'", (ultimo_account,))
                setup.commit()
            r = client.get("/me", headers=auth_header)
            check("ultimo_digest prende la data account quando e' piu' recente",
                  datetime.fromisoformat(r.json()["ultimo_digest"]), ultimo_account)
            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE users SET last_digest_at = NULL "
                                "WHERE email = 'pref@test.dev'")
                    cur.execute("UPDATE user_clusters SET is_paused = false WHERE id = %s",
                                (ricerca_sched_id,))
                setup.commit()
            check("una seconda eredità esplicita resta idempotente",
                  put_sched(frequency=None).status_code, 204)

            check("disiscrizione: 204",
                  client.delete(f"/me/cluster/{cluster_id}",
                                headers=auth_header).status_code, 204)
            check("dopo la disiscrizione non ci sono cluster",
                  client.get("/me/cluster", headers=auth_header).json(), [])

            print("\n— due ruoli nello stesso cluster —")
            ruoli = []
            for ruolo in ("HR Manager", "HRBP"):
                filtri_ruolo = {"target_role": ruolo}
                if ruolo == "HRBP":
                    filtri_ruolo.update(frequency="daily", send_hour_local=6,
                                        timezone="Europe/Rome")
                r = client.post("/me/ricerca", headers=auth_header,
                                json={"family": "Human Resources", "country": "IT",
                                      "filtri": filtri_ruolo})
                check(f"ricerca {ruolo}: 201", r.status_code, 201)
                ruoli.append(r.json())
            check("POST ereditata restituisce le due date nulle",
                  (ruoli[0]["next_digest_at"], ruoli[0]["last_digest_at"]), (None, None))
            check("POST con override restituisce lo slot appena salvato",
                  (ruoli[1]["next_digest_at"] is not None, ruoli[1]["last_digest_at"]),
                  (True, None))
            check("ogni ricerca ha un UUID proprio", ruoli[0]["id"] != ruoli[1]["id"], True)
            check("il cluster resta condiviso",
                  ruoli[0]["cluster_id"] == ruoli[1]["cluster_id"] == cluster_id, True)
            r = client.post("/me/ricerca", headers=auth_header,
                            json={"family": "Human Resources", "country": "IT",
                                  "filtri": {"target_role": " hrbp "}})
            check("stesso ruolo normalizzato resta idempotente", r.json()["id"], ruoli[1]["id"])
            check("POST senza scheduling conserva lo slot dell'override esistente",
                  r.json()["next_digest_at"], ruoli[1]["next_digest_at"])
            r = client.get("/me/cluster", headers=auth_header)
            check("entrambe le ricerche si rileggono", len(r.json()), 2)
            check("i ruoli non si sovrascrivono",
                  {x["filtri"]["target_role"] for x in r.json()}, {"HR Manager", "HRBP"})
            r = client.put(f"/me/cluster/{ruoli[0]['id']}",
                         json={"target_role": "HRBP"}, headers=auth_header)
            check("cambiare ruolo verso uno esistente e' un 409", r.status_code, 409)
            r = client.put(f"/me/cluster/{ruoli[0]['id']}",
                         json={"target_role": "HR Director"}, headers=auth_header)
            check("modificare solo la ricerca scelta: 204", r.status_code, 204)
            check("il vecchio cluster_id e' ambiguo con due ricerche",
                  client.put(f"/me/cluster/{cluster_id}", json={},
                             headers=auth_header).status_code, 409)
            check("elimino solo HR Manager",
                  client.delete(f"/me/cluster/{ruoli[0]['id']}",
                                headers=auth_header).status_code, 204)
            r = client.get("/me/cluster", headers=auth_header)
            check("HRBP resta indipendente",
                  [(x["id"], x["filtri"]["target_role"]) for x in r.json()],
                  [(ruoli[1]["id"], "HRBP")])
            check("elimino anche HRBP",
                  client.delete(f"/me/cluster/{ruoli[1]['id']}",
                                headers=auth_header).status_code, 204)

            print("\n— feedback, tracker e storico delle offerte —")

            def stato(base, **modifiche):
                corpo = {
                    "base_version": base, "salvata": False, "scartata": False,
                    "motivo_scarto": None, "in_archivio": False,
                    "candidatura": "not_started", "salvata_il": None,
                    "scartata_il": None, "archiviata_il": None,
                    "applicata_il": None,
                }
                corpo.update(modifiche)
                return corpo

            def match_nel_json(risposta):
                return {item["id"] for item in risposta.json()["offerte"]}

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("SELECT id FROM users WHERE email = 'pref@test.dev'")
                    owner_id = cur.fetchone()[0]
                    ricerche_stato = {}
                    for ruolo, soglia in (("HR Manager", 70), ("HRBP", 90)):
                        cur.execute(
                            "INSERT INTO user_clusters (user_id,cluster_id,target_role,min_score) "
                            "VALUES (%s,%s,%s,%s) RETURNING id", (owner_id, cluster_id, ruolo, soglia))
                        ricerche_stato[ruolo] = str(cur.fetchone()[0])
                    cur.execute(
                        "INSERT INTO users (email,plan,subscription_status,delivery_channels,"
                        "frequency,timezone) VALUES ('api-state-other@test.dev','basic','active',"
                        "'{email}','daily','UTC') RETURNING id")
                    other_id = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO user_clusters (user_id,cluster_id,target_role) "
                        "VALUES (%s,%s,'HR Manager') RETURNING id", (other_id, cluster_id))
                    ricerca_altri = str(cur.fetchone()[0])

                    job_stato = {}
                    match_stato = {}

                    def fixture_match(chiave, score, dichiarata, *, status="active", purged=False):
                        sid = f"api-state-{chiave}"
                        url = f"https://acme.example/{sid}"
                        cur.execute(
                            "INSERT INTO jobs (source,source_job_id,url,canonical_url,domain_derived,"
                            "title,title_normalized,organization,date_posted,date_posted_declared,"
                            "countries,raw,link_kind,status,expired_at,purged_at,first_seen_at,"
                            "last_seen_alive_at) VALUES ('fantastic',%s,%s,%s,'acme.example',"
                            "%s,%s,'Acme',now() - interval '2 days',%s,ARRAY['IT'],'{}'::jsonb,"
                            "'career_site',%s,CASE WHEN %s = 'active' THEN NULL ELSE now() END,"
                            "CASE WHEN %s THEN now() ELSE NULL END,now() - interval '5 days',"
                            "now() - interval '1 day') RETURNING id",
                            (sid, url, url, f"Offerta {chiave}", f"offerta {chiave}", dichiarata,
                             status, status, purged))
                        jid = cur.fetchone()[0]
                        job_stato[chiave] = str(jid)
                        cur.execute("INSERT INTO job_clusters (job_id,cluster_id) VALUES (%s,%s)",
                                    (jid, cluster_id))
                        cur.execute(
                            "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model,"
                            "evaluated_at,analysis) VALUES (%s,%s,%s,'motivo di test',70,'test',"
                            "now() - make_interval(mins => %s::int),"
                            "'{\"perche\":\"analisi di test\"}'::jsonb) RETURNING id",
                            (owner_id, jid, score, len(match_stato)))
                        match_stato[chiave] = str(cur.fetchone()[0])

                    fixture_match("m1", 85, True)
                    cur.execute(
                        "UPDATE jobs SET ai_requirements_summary = 'requisiti dalla fonte', "
                        "ai_core_responsibilities = 'responsabilita dalla fonte', "
                        "raw = '{\"descrizione\":\"testo integrale dalla fonte\"}'::jsonb "
                        "WHERE id = %s", (job_stato["m1"],))
                    fixture_match("m2", 95, False)
                    cur.execute(
                        "UPDATE jobs SET raw = '{\"ats\":{\"description\":\"<p>responsabilita annidata</p>\"}}'::jsonb, "
                        "url = 'https://it.indeed.com/viewjob?jk=m2', "
                        "canonical_url = 'https://it.indeed.com/viewjob?jk=m2', "
                        "domain_derived = 'it.indeed.com', link_kind = 'job_board', "
                        "org_linkedin_slug = NULL WHERE id = %s", (job_stato["m2"],))
                    fixture_match("m3", 90, None)
                    fixture_match("m4", 92, None, status="expired", purged=True)
                    fixture_match("m5", 88, False)
                    fixture_match("m6", 60, True)
                    fixture_match("m7", 90, None)
                    for chiave, sql in {
                        "m2": "UPDATE matches SET saved_at = now() - interval '3 days', "
                              "action_version = 1, user_action_at = now() - interval '3 days'",
                        "m3": "UPDATE matches SET dismissed_at = now() - interval '2 days', "
                              "dismiss_reason = 'company', action_version = 1, "
                              "user_action_at = now() - interval '2 days'",
                        "m4": "UPDATE matches SET saved_at = now() - interval '4 days', "
                              "application_status = 'applied', applied_at = now() - interval '3 days', "
                              "action_version = 1, user_action_at = now() - interval '2 days'",
                        "m5": "UPDATE matches SET archived_at = now() - interval '2 days', "
                              "action_version = 1, user_action_at = now() - interval '2 days'",
                    }.items():
                        cur.execute(sql + " WHERE id = %s", (match_stato[chiave],))
                    cur.execute("UPDATE jobs SET date_valid_through = now() + interval '2 days' "
                                "WHERE id = %s", (job_stato["m2"],))
                    cur.execute("DELETE FROM user_cvs WHERE user_id = %s", (owner_id,))
                    for stato_cv, famiglia, chiave in (
                            ("superseded", "Legacy Family", "cv/test/legacy"),
                            ("active", "Active Family", "cv/test/active")):
                        cur.execute(
                            "INSERT INTO user_cvs (user_id,storage_key,status,families,seniority,"
                            "skills,languages,encryption_algo,encrypted_dek,nonce,auth_tag,"
                            "kek_version) VALUES (%s,%s,%s,ARRAY[%s],'5-10',"
                            "ARRAY['recruiting'],'[\"English\"]'::jsonb,'aes-256-gcm',"
                            "decode(repeat('ab',32),'hex'),decode(repeat('cd',12),'hex'),"
                            "decode(repeat('ef',16),'hex'),1) RETURNING id",
                            (owner_id, chiave, stato_cv, famiglia))
                        if stato_cv == "superseded":
                            cv_match = cur.fetchone()[0]
                    cur.execute("UPDATE matches SET cv_id = %s WHERE id = %s",
                                (cv_match, match_stato["m1"]))
                    cur.execute(
                        "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model) "
                        "VALUES (%s,%s,80,'altro utente',70,'test') RETURNING id",
                        (other_id, job_stato["m7"]))
                    match_altri = str(cur.fetchone()[0])
                setup.commit()

            r = client.get("/me/offerte", params={"vista": "match", "limite": 100},
                           headers=auth_header)
            check("vista match: soglia corrente, job attivo e stati personali",
                  (r.status_code, r.json()["totale"], match_nel_json(r)),
                  (200, 3, {match_stato["m1"], match_stato["m2"], match_stato["m7"]}))
            check("valutate conserva il conteggio completo, non quello della pagina",
                  r.json()["valutate"], 7)
            check("home: offerte ordinate dal punteggio più alto",
                  [item["id"] for item in r.json()["offerte"]],
                  [match_stato["m2"], match_stato["m7"], match_stato["m1"]])
            with psycopg.connect(database_url()) as setup, setup.cursor() as cur:
                cur.execute("UPDATE jobs SET date_valid_through = now() - interval '1 day' "
                            "WHERE id = %s", (job_stato["m7"],))
            r_scaduta = client.get("/me/offerte", params={"vista": "match", "limite": 100},
                                   headers=auth_header)
            check("home: un'offerta scaduta non viene mostrata",
                  match_nel_json(r_scaduta), {match_stato["m1"], match_stato["m2"]})
            with psycopg.connect(database_url()) as setup, setup.cursor() as cur:
                cur.execute("UPDATE jobs SET date_valid_through = NULL WHERE id = %s",
                            (job_stato["m7"],))
            offerte_per_id = {item["id"]: item for item in r.json()["offerte"]}
            primo_json = offerte_per_id[match_stato["m1"]]
            check("fonte, stato e date reali nella risposta",
                  (primo_json["fonte"], primo_json["offerta_status"],
                   bool(primo_json["pubblicata_il"]),
                   bool(primo_json["prima_rilevazione"]), bool(primo_json["ultima_verifica"])),
                  ("fantastic", "active", True, True, True))
            check("il dominio di una job board non diventa il logo aziendale",
                  offerte_per_id[match_stato["m2"]]["logo"], None)
            check("data fallback non esposta come pubblicazione",
                  offerte_per_id[match_stato["m2"]]["pubblicata_il"], None)
            check("data legacy senza prova non esposta come pubblicazione",
                  offerte_per_id[match_stato["m7"]]["pubblicata_il"], None)
            check("HR Manager e HRBP restano distinti sullo stesso match",
                  (set(primo_json["ricerca_ids"]),
                   set(primo_json["ricerca_eleggibile_ids"])),
                  (set(ricerche_stato.values()), {ricerche_stato["HR Manager"]}))
            check("i salvati possono restare nella vista match",
                  offerte_per_id[match_stato["m2"]]["salvata"], True)
            check("valid_through e campi tracker flat presenti anche in lista",
                  (bool(offerte_per_id[match_stato["m2"]]["valid_through"]),
                   primo_json["valid_through"], primo_json["note"],
                   primo_json["prossima_azione"], primo_json["prossima_azione_il"]),
                  (True, None, None, None, None))
            check("colloqui e documenti non appesantiscono la lista",
                  ("colloqui" in primo_json, "documenti" in primo_json), (False, False))

            attese_viste = {
                "azioni": {"m2", "m3", "m4", "m5"},
                "salvate": {"m2", "m4"},
                "candidature": {"m4"},
                "scartate": {"m3"},
                "archivio": {"m5"},
            }
            for vista, chiavi in attese_viste.items():
                r = client.get("/me/offerte", params={"vista": vista, "limite": 100},
                               headers=auth_header)
                attesi = {match_stato[chiave] for chiave in chiavi}
                check(f"vista {vista}: indipendente dalla soglia corrente",
                      (r.status_code, r.json()["totale"], match_nel_json(r)),
                      (200, len(attesi), attesi))
            r = client.get("/me/offerte", params={"vista": "archivio", "limite": 100},
                           headers=auth_header)
            check("archivio utente diverso dalla lapide jobs.purged_at",
                  [(item["in_archivio"], item["archiviata"]) for item in r.json()["offerte"]],
                  [(True, False)])

            for ricerca, chiavi in (("HR Manager", {"m1", "m2", "m7"}),
                                    ("HRBP", {"m2", "m7"})):
                r = client.get("/me/offerte", params={
                    "vista": "match", "ricerca_id": ricerche_stato[ricerca], "limite": 100},
                    headers=auth_header)
                check(f"filtro corrente per {ricerca}", match_nel_json(r),
                      {match_stato[chiave] for chiave in chiavi})
            r = client.get("/me/offerte", params={
                "vista": "azioni", "ricerca_id": ricerche_stato["HRBP"], "limite": 100},
                headers=auth_header)
            check("filtro storico: associazione corrente, non eleggibilita'",
                  r.json()["totale"], 4)
            check("vecchio cluster UUID ambiguo con due ricerche: 409",
                  client.get("/me/offerte", params={"ricerca_id": cluster_id},
                             headers=auth_header).status_code, 409)
            check("ricerca di un altro utente: 404 senza rivelare dati",
                  client.get("/me/offerte", params={"ricerca_id": ricerca_altri},
                             headers=auth_header).status_code, 404)
            check("vista sconosciuta: 422",
                  client.get("/me/offerte", params={"vista": "inesistente"},
                             headers=auth_header).status_code, 422)
            check("limite oltre 100: 422",
                  client.get("/me/offerte", params={"limite": 101},
                             headers=auth_header).status_code, 422)
            check("offset negativo: 422",
                  client.get("/me/offerte", params={"offset": -1},
                             headers=auth_header).status_code, 422)

            r = client.get(f"/me/offerte/{match_stato['m4']}", headers=auth_header)
            dettaglio_candidatura = r.json()
            check("dettaglio di candidatura scaduta/purgata resta aperto e porta id",
                  (r.status_code, dettaglio_candidatura.get("id"),
                   dettaglio_candidatura.get("offerta_status"),
                   dettaglio_candidatura.get("archiviata"),
                   dettaglio_candidatura.get("in_archivio")),
                  (200, match_stato["m4"], "expired", True, False))
            applicata_prima = dettaglio_candidatura["applicata_il"]
            r = client.get(f"/me/offerte/{match_stato['m1']}", headers=auth_header)
            check("evidenza e analisi usano il CV del match, non quello attivo",
                  (r.status_code, r.json()["evidence"]["ruolo"]["cercato"],
                   r.json()["colloqui"], r.json()["documenti"]),
                  (200, "Legacy Family", [], []))
            check("il brief usa i campi fonte del job, non il testo inventato",
                  r.json()["annuncio"],
                  {"responsabilita": "responsabilita dalla fonte",
                   "requisiti": "requisiti dalla fonte", "benefit": None, "orario": None})
            r = client.get(f"/me/offerte/{match_stato['m2']}", headers=auth_header)
            check("il brief legge anche raw.ats.description",
                  r.json()["annuncio"]["responsabilita"], "responsabilita annidata")
            check("dettaglio del match altrui: 404",
                  client.get(f"/me/offerte/{match_altri}", headers=auth_header).status_code, 404)
            check("dettaglio inesistente: 404",
                  client.get("/me/offerte/00000000-0000-0000-0000-000000000000",
                             headers=auth_header).status_code, 404)

            check("mutation senza sessione: 401",
                  client.put(f"/me/offerte/{match_stato['m1']}/stato", json=stato(0)).status_code,
                  401)
            check("mutation sul match altrui: 404",
                  client.put(f"/me/offerte/{match_altri}/stato", json=stato(0),
                             headers=auth_header).status_code, 404)
            check("mutation inesistente: 404",
                  client.put("/me/offerte/00000000-0000-0000-0000-000000000000/stato",
                             json=stato(0), headers=auth_header).status_code, 404)
            incompleto = stato(0, salvata=True)
            del incompleto["in_archivio"]
            check("stato desiderato incompleto: 422",
                  client.put(f"/me/offerte/{match_stato['m1']}/stato", json=incompleto,
                             headers=auth_header).status_code, 422)
            futuro = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
            check("timestamp legacy futuro: 422",
                  client.put(f"/me/offerte/{match_stato['m6']}/stato",
                             json=stato(0, salvata=True, salvata_il=futuro),
                             headers=auth_header).status_code, 422)
            check("salvataggio e scarto insieme: 422",
                  client.put(f"/me/offerte/{match_stato['m6']}/stato",
                             json=stato(0, salvata=True, scartata=True, motivo_scarto="target"),
                             headers=auth_header).status_code, 422)

            legacy_salvato = (datetime.now(timezone.utc) - timedelta(days=2)).replace(microsecond=0)
            corpo_salvato = stato(0, salvata=True, salvata_il=legacy_salvato.isoformat())
            r = client.put(f"/me/offerte/{match_stato['m1']}/stato",
                           json=corpo_salvato, headers=auth_header)
            check("replay legacy a versione zero accetta il timestamp passato",
                  (r.status_code, r.json()["salvata"], r.json()["action_version"],
                   bool(r.json()["azioni_aggiornate_il"])), (200, True, 1, True))
            check("salvata_il e' il timestamp di prima transizione",
                  seen_by_other("SELECT saved_at = %s FROM matches WHERE id = %s",
                                (legacy_salvato, match_stato["m1"])), True)
            prima_risposta = r.json()
            r = client.put(f"/me/offerte/{match_stato['m1']}/stato",
                           json=corpo_salvato, headers=auth_header)
            check("replay dello stesso stato e' idempotente anche con base_version zero",
                  (r.status_code, r.json()["action_version"], r.json()["azioni_aggiornate_il"]),
                  (200, prima_risposta["action_version"],
                   prima_risposta["azioni_aggiornate_il"]))
            r = client.put(f"/me/offerte/{match_stato['m1']}/stato",
                           json=stato(0, salvata=True, in_archivio=True), headers=auth_header)
            check("versione obsoleta con stato diverso: 409 e stato canonico",
                  (r.status_code, r.json()["offerta"]["action_version"],
                   r.json()["offerta"]["salvata"], r.json()["offerta"]["in_archivio"]),
                  (409, 1, True, False))
            check("timestamp client su una modifica normale: 422",
                  client.put(f"/me/offerte/{match_stato['m1']}/stato",
                             json=stato(1, salvata=True, in_archivio=True,
                                        salvata_il=legacy_salvato.isoformat()),
                             headers=auth_header).status_code, 422)
            check("il 422 non incrementa action_version",
                  seen_by_other("SELECT action_version FROM matches WHERE id = %s",
                                (match_stato["m1"],)), 1)

            legacy_scarto = (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
            r = client.put(f"/me/offerte/{match_stato['m6']}/stato",
                           json=stato(0, scartata=True, motivo_scarto="target",
                                      scartata_il=legacy_scarto.isoformat()),
                           headers=auth_header)
            check("scarto con motivo company/target e timestamp legacy",
                  (r.status_code, r.json()["scartata"], r.json()["motivo_scarto"],
                   r.json()["action_version"]), (200, True, "target", 1))
            r = client.put(f"/me/offerte/{match_stato['m6']}/stato",
                           json=stato(1, salvata=True), headers=auth_header)
            check("salvare riapre esplicitamente uno scarto",
                  (r.status_code, r.json()["salvata"], r.json()["scartata"],
                   r.json()["motivo_scarto"], r.json()["action_version"]),
                  (200, True, False, None, 2))

            r = client.put(f"/me/offerte/{match_stato['m4']}/stato",
                           json=stato(1, salvata=True, candidatura="interview"),
                           headers=auth_header)
            check("applied_at non cambia da applied a interview",
                  (r.status_code, r.json()["action_version"], r.json()["applicata_il"],
                   r.json()["salvata"]), (200, 2, applicata_prima, True))
            r = client.put(f"/me/offerte/{match_stato['m4']}/stato",
                           json=stato(2, salvata=True, candidatura="offer"),
                           headers=auth_header)
            check("applied_at non cambia neppure a offer",
                  (r.status_code, r.json()["action_version"], r.json()["applicata_il"]),
                  (200, 3, applicata_prima))
            r = client.put(f"/me/offerte/{match_stato['m4']}/stato",
                           json=stato(3, salvata=True), headers=auth_header)
            check("reset candidatura preserva il salvataggio",
                  (r.status_code, r.json()["candidatura"], r.json()["applicata_il"],
                   r.json()["salvata"], r.json()["action_version"]),
                  (200, "not_started", None, True, 4))

            legacy_applicazione = (datetime.now(timezone.utc) - timedelta(days=3)) \
                .replace(microsecond=0)
            r = client.put(f"/me/offerte/{match_stato['m7']}/stato",
                           json=stato(0, candidatura="applied",
                                      applicata_il=legacy_applicazione.isoformat()),
                           headers=auth_header)
            check("prima candidatura self-reported dal replay legacy",
                  (r.status_code, r.json()["candidatura"], r.json()["action_version"]),
                  (200, "applied", 1))
            applicata_legacy_json = r.json()["applicata_il"]
            check("applicata_il legacy persistito senza inventare la data",
                  seen_by_other("SELECT applied_at = %s FROM matches WHERE id = %s",
                                (legacy_applicazione, match_stato["m7"])), True)
            r = client.put(f"/me/offerte/{match_stato['m7']}/stato",
                           json=stato(1, candidatura="interview"), headers=auth_header)
            check("passaggio successivo conserva la prima candidatura legacy",
                  (r.status_code, r.json()["applicata_il"], r.json()["action_version"]),
                  (200, applicata_legacy_json, 2))

            finali = {
                "match": {"m1", "m2"},
                "salvate": {"m1", "m2", "m4", "m6"},
                "candidature": {"m7"},
                "scartate": {"m3"},
                "archivio": {"m5"},
                "azioni": {"m1", "m2", "m3", "m4", "m5", "m6", "m7"},
            }
            for vista, chiavi in finali.items():
                r = client.get("/me/offerte", params={"vista": vista, "limite": 100},
                               headers=auth_header)
                check(f"viste finali: {vista}", match_nel_json(r),
                      {match_stato[chiave] for chiave in chiavi})

            storico = []
            dimensioni = []
            for offset in (0, 3, 6):
                r = client.get("/me/offerte", params={
                    "vista": "azioni", "limite": 3, "offset": offset}, headers=auth_header)
                dimensioni.append(len(r.json()["offerte"]))
                storico.extend(item["id"] for item in r.json()["offerte"])
                check(f"totale stabile della vista a offset {offset}", r.json()["totale"], 7)
            check("paginazione stabile: dimensioni delle pagine", dimensioni, [3, 3, 1])
            check("paginazione recupera tutto lo storico senza duplicati",
                  (len(storico), len(set(storico)), set(storico)),
                  (7, 7, set(match_stato.values())))

            prossima = (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0)
            corpo_tracker = stato(1, salvata=True, note="Portare il CV mirato",
                                  contatto="Mara Recruiter, mara@example.com",
                                  prossima_azione="Chiamare il recruiter",
                                  prossima_azione_il=prossima.isoformat())
            r = client.put(f"/me/offerte/{match_stato['m1']}/stato",
                           json=corpo_tracker, headers=auth_header)
            check("note, contatto e prossima azione versionate nel tracker remoto",
                  (r.status_code, r.json()["note"], r.json()["contatto"],
                   r.json()["prossima_azione"], r.json()["prossima_azione_il"],
                   r.json()["action_version"]),
                  (200, "Portare il CV mirato", "Mara Recruiter, mara@example.com",
                   "Chiamare il recruiter", prossima.isoformat(), 2))
            r = client.put(f"/me/offerte/{match_stato['m1']}/stato",
                           json=corpo_tracker, headers=auth_header)
            check("replay tracker identico resta idempotente con base legacy",
                  (r.status_code, r.json()["action_version"]), (200, 2))
            r = client.put(f"/me/offerte/{match_stato['m1']}/stato",
                           json=stato(2, salvata=True), headers=auth_header)
            check("campi tracker omessi dal client restano invariati",
                  (r.status_code, r.json()["note"], r.json()["contatto"],
                   r.json()["action_version"]),
                  (200, "Portare il CV mirato", "Mara Recruiter, mara@example.com", 2))
            r = client.put(f"/me/offerte/{match_stato['m1']}/stato",
                           json=stato(1, salvata=True, note="conflitto"),
                           headers=auth_header)
            check("conflitto tracker restituisce l'offerta canonica",
                  (r.status_code, r.json()["offerta"]["action_version"],
                   r.json()["offerta"]["note"]),
                  (409, 2, "Portare il CV mirato"))
            r = client.put(f"/me/offerte/{match_stato['m3']}/stato",
                           json=stato(1, scartata=True, motivo_scarto="skills"),
                           headers=auth_header)
            check("motivo di scarto arricchito accettato dall'API",
                  (r.status_code, r.json()["motivo_scarto"], r.json()["action_version"]),
                  (200, "skills", 2))

            inizio_colloquio = datetime.now(timezone.utc) + timedelta(days=2)
            r = client.post(f"/me/offerte/{match_stato['m2']}/colloqui",
                            json={"inizio": inizio_colloquio.isoformat(),
                                  "note": "Primo giro", "promemoria_minuti": 60},
                            headers=auth_header)
            check("creazione colloquio incrementa la versione del match",
                  (r.status_code, r.json()["action_version"], len(r.json()["colloqui"])),
                  (200, 2, 1))
            colloquio_id = r.json()["colloqui"][0]["id"]
            r = client.post(f"/me/offerte/{match_stato['m2']}/colloqui",
                            json={"inizio": (inizio_colloquio + timedelta(days=1)).isoformat(),
                                  "note": None, "promemoria_minuti": None},
                            headers=auth_header)
            secondo_colloquio = r.json()["colloqui"][-1]["id"]
            check("colloqui multipli sullo stesso match",
                  (r.status_code, r.json()["action_version"], len(r.json()["colloqui"])),
                  (200, 3, 2))
            r = client.put(f"/me/offerte/{match_stato['m2']}/colloqui/{colloquio_id}",
                           json={"inizio": (inizio_colloquio + timedelta(days=2)).isoformat(),
                                 "note": "Spostato", "promemoria_minuti": 30},
                           headers=auth_header)
            aggiornato = next(c for c in r.json()["colloqui"] if c["id"] == colloquio_id)
            check("aggiornamento colloquio incrementa la versione",
                  (r.status_code, r.json()["action_version"], aggiornato["note"]),
                  (200, 4, "Spostato"))
            r = client.delete(
                f"/me/offerte/{match_stato['m2']}/colloqui/{secondo_colloquio}",
                headers=auth_header)
            check("cancellazione colloquio incrementa la versione",
                  (r.status_code, r.json()["action_version"], len(r.json()["colloqui"])),
                  (200, 5, 1))
            check("colloquio su match altrui non leggibile o creabile",
                  client.post(f"/me/offerte/{match_altri}/colloqui",
                              json={"inizio": inizio_colloquio.isoformat()},
                              headers=auth_header).status_code, 404)

            blob_documenti: dict[str, bytes] = {}
            salva_originale = app_module.storage.salva
            leggi_originale = app_module.storage.leggi
            elimina_originale = app_module.storage.elimina
            vecchia_kek = os.environ.get("CV_KEK")
            os.environ["CV_KEK"] = secrets.token_hex(32)
            app_module.storage.salva = lambda chiave, dati: blob_documenti.__setitem__(chiave, dati)
            app_module.storage.leggi = lambda chiave: blob_documenti[chiave]
            app_module.storage.elimina = lambda chiave: blob_documenti.pop(chiave, None)
            try:
                contenuto = b"%PDF-1.4\nlettera di prova"
                r = client.post(f"/me/offerte/{match_stato['m2']}/documenti",
                                files={"file": ("lettera.pdf", contenuto, "application/pdf")},
                                headers=auth_header)
                check("upload documento cifrato e versionato",
                      (r.status_code, r.json()["action_version"], len(r.json()["documenti"]),
                       len(blob_documenti)), (200, 6, 1, 1))
                documento = r.json()["documenti"][0]
                check("metadati documento nel contratto app",
                      (documento["nome"], documento["mime"], documento["dimensione"],
                       bool(documento["creato_il"])),
                      ("lettera.pdf", "application/pdf", len(contenuto), True))
                documento_url = f"/me/offerte/{match_stato['m2']}/documenti/{documento['id']}"
                check("download documento richiede autenticazione",
                      client.get(documento_url).status_code, 401)
                r = client.get(documento_url, headers=auth_header)
                check("download autenticato decifra e protegge la risposta",
                      (r.status_code, r.content, r.headers["cache-control"],
                       r.headers["x-content-type-options"],
                       r.headers["content-disposition"].startswith("attachment")),
                      (200, contenuto, "no-store", "nosniff", True))
                r = client.post(f"/me/offerte/{match_stato['m2']}/documenti",
                                files={"file": ("pagina.html", b"<script>", "text/html")},
                                headers=auth_header)
                check("MIME non sicuro rifiutato senza mutation",
                      (r.status_code, seen_by_other(
                          "SELECT action_version FROM matches WHERE id = %s",
                          (match_stato["m2"],))), (422, 6))
                r = client.delete(documento_url, headers=auth_header)
                check("delete documento versiona e rimuove il blob",
                      (r.status_code, r.json()["action_version"], r.json()["documenti"],
                       blob_documenti), (200, 7, [], {}))
            finally:
                app_module.storage.salva = salva_originale
                app_module.storage.leggi = leggi_originale
                app_module.storage.elimina = elimina_originale
                if vecchia_kek is None:
                    del os.environ["CV_KEK"]
                else:
                    os.environ["CV_KEK"] = vecchia_kek

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE matches SET applied_at = user_action_at WHERE id = %s",
                                (match_stato["m7"],))
                setup.commit()
            r = client.get("/me/riepilogo-settimanale", headers=auth_header)
            riepilogo = r.json()
            check("riepilogo deterministico: pipeline e candidature della settimana",
                  (r.status_code, riepilogo["pipeline"]["interview"],
                   riepilogo["candidature_questa_settimana"]), (200, 1, 1))
            check("riepilogo collega colloqui, azioni, scadenze e motivi reali",
                  ([o["id"] for o in riepilogo["colloqui_prossimi"]],
                   [o["id"] for o in riepilogo["azioni_prossime"]],
                   [o["id"] for o in riepilogo["scadenze"]],
                   riepilogo["motivi_scarto"]),
                  ([match_stato["m2"]], [match_stato["m1"]], [match_stato["m2"]],
                   [{"motivo": "skills", "count": 1}]))
            check("digest_ricevuti conta soltanto consegne reali",
                  isinstance(riepilogo["digest_ricevuti"], int), True)
            offerte_riepilogo = [offerta for nome in (
                "colloqui_prossimi", "azioni_prossime", "scadenze")
                for offerta in riepilogo[nome]]
            check("gli array del riepilogo contengono offerte piatte, non wrapper",
                  all({"id", "titolo", "azienda", "punteggio", "action_version"}
                      <= offerta.keys() and "offerta" not in offerta
                      for offerta in offerte_riepilogo), True)
            r2 = client.get("/me/riepilogo-settimanale", headers=auth_header)
            check("due letture consecutive del riepilogo sono identiche",
                  r2.json(), riepilogo)

            from nivult.matching import llm as llm_dettaglio
            glm_originale = llm_dettaglio.GLM
            valuta_originale = llm_dettaglio.valuta_offerta
            analisi_originale = llm_dettaglio.analizza_allineamento
            chiamate = {}

            class GLMFinto:
                model = "modello-finto"
                source = "test"

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

            def valuta_finta(modello, profilo_testo, offerta, **kw):
                raise AssertionError("il dettaglio non deve ricalcolare il punteggio")

            def analisi_finta(modello, profilo_testo, offerta, **kw):
                chiamate.update(lingua=kw.get("lingua"), profilo=profilo_testo,
                                paesi=offerta["countries"],
                                descrizione=offerta["ai_core_responsibilities"])
                return {"perche": "Nuova analisi italiana", "pros": ["Workday"],
                        "cons": [], "responsabilita": "Gestione dei reclami",
                        "requisiti": "Contratti collettivi", "benefit": None,
                        "lang": "Italian", "rubrica": llm_dettaglio.VERSIONE_ANALISI}

            try:
                llm_dettaglio.GLM = GLMFinto
                llm_dettaglio.valuta_offerta = valuta_finta
                llm_dettaglio.analizza_allineamento = analisi_finta
                os.environ["GLM_API_KEY"] = "finto"
                with psycopg.connect(database_url()) as setup, setup.cursor() as cur:
                    cur.execute("UPDATE user_cvs SET location_city = 'Rome', "
                                "location_country = 'IT', relocation_open = true "
                                "WHERE user_id = %s", (owner_id,))
                    cur.execute("UPDATE jobs SET raw = %s::jsonb WHERE id = %s",
                                ('{"description":"Country: United States of America '
                                 'Location: US-NY-ROME-104 Manage collective bargaining"}',
                                 job_stato["m1"]))
                r = client.get(f"/me/offerte/{match_stato['m1']}", params={"lingua": "it"},
                               headers=auth_header)
                check("vecchio match: il punteggio non cambia all'apertura",
                      (r.status_code, r.json()["punteggio"], r.json()["paese"],
                       r.json()["analisi"]), (200, 85, "US",
                       {"pro": ["Workday"], "attenzioni": []}))
                check("vecchio match: lingua, località CV e fonte raggiungono il modello",
                      (chiamate["lingua"], chiamate["paesi"],
                       "Rome · IT" in chiamate["profilo"],
                       "Disponibile a trasferirsi" in chiamate["profilo"],
                       "Manage collective bargaining" in chiamate["descrizione"]),
                      ("Italian", ["US"], True, True, True))
                check("vecchio match: punteggio e motivo persistono senza perdere le note",
                      (seen_by_other("SELECT score || '/' || reason "
                                     "FROM matches WHERE id = %s", (match_stato["m1"],)),
                       r.json()["note"]), ("85/motivo di test", "Portare il CV mirato"))
                check("il dettaglio usa la sintesi italiana, non il testo integrale",
                      (r.json()["annuncio"]["responsabilita"],
                       r.json()["annuncio"]["requisiti"]),
                      ("Gestione dei reclami", "Contratti collettivi"))
                r = client.get(f"/me/offerte/{match_stato['m3']}", params={"lingua": "it"},
                               headers=auth_header)
                check("senza descrizione: score stabile e nessuna mansione inventata",
                      (r.status_code, r.json()["punteggio"], r.json()["motivo"],
                       r.json()["annuncio"]["responsabilita"], r.json()["analisi"]),
                      (200, 90, "motivo di test", None,
                       {"pro": [], "attenzioni": []}))
            finally:
                llm_dettaglio.GLM = glm_originale
                llm_dettaglio.valuta_offerta = valuta_originale
                llm_dettaglio.analizza_allineamento = analisi_originale
                os.environ.pop("GLM_API_KEY", None)

            from nivult.matching import lettera as lettera_dettaglio
            genera_lettera_originale = lettera_dettaglio.genera_lettera
            chiamate_lettere = []

            def lettera_finta(modello, profilo_testo, offerta, **kw):
                chiamate_lettere.append(kw["lingua"])
                return (f"Lettera in {kw['lingua']} per {offerta['title']} "
                        f"presso {offerta['organization']}.")

            try:
                lettera_dettaglio.genera_lettera = lettera_finta
                os.environ["GLM_API_KEY"] = "finto"
                r = client.post(f"/me/offerte/{match_stato['m2']}/lettera",
                                params={"lingua": "it"}, headers=auth_header)
                check("lettera generata sul profilo del proprietario del match",
                      (r.status_code, r.json()["lettera"], r.json()["lingua"],
                       r.headers["cache-control"]),
                      (200, "Lettera in Italian per Offerta m2 presso Acme.",
                       "Italian", "no-store"))
                prima_lettera = r.json()
                r = client.post(f"/me/offerte/{match_stato['m2']}/lettera",
                                params={"lingua": "it"}, headers=auth_header)
                check("seconda apertura: lettera dalla cache, senza rigenerarla",
                      (r.json(), len(chiamate_lettere)), (prima_lettera, 1))
                r = client.post(f"/me/offerte/{match_stato['m2']}/lettera",
                                params={"lingua": "en"}, headers=auth_header)
                check("la cache della lettera distingue la lingua",
                      (r.json()["lingua"], len(chiamate_lettere)), ("English", 2))
                r = client.post(f"/me/offerte/{match_stato['m3']}/lettera",
                                params={"lingua": "it"}, headers=auth_header)
                check("senza fonte o dati sufficienti: nessuna lettera generica",
                      r.status_code, 409)
            finally:
                lettera_dettaglio.genera_lettera = genera_lettera_originale
                os.environ.pop("GLM_API_KEY", None)

            from nivult.matching import colloquio as colloquio_dettaglio
            genera_colloquio_originale = colloquio_dettaglio.genera_colloquio
            chiamate_colloquio = []

            def colloquio_finto(modello, profilo_testo, offerta, **kw):
                chiamate_colloquio.append(kw["lingua"])
                return {
                    "spunti": [f"Spunto specifico in {kw['lingua']}"],
                    "domande": [{
                        "domanda": "Quale progetto hai seguito direttamente?",
                        "preparazione": "Scegli un caso coerente col tuo percorso.",
                        "risposta": "Distingui il tuo contributo da quello del team.",
                    }],
                }

            try:
                colloquio_dettaglio.genera_colloquio = colloquio_finto
                os.environ["GLM_API_KEY"] = "finto"
                r = client.post(f"/me/offerte/{match_stato['m2']}/colloquio",
                                params={"lingua": "it"}, headers=auth_header)
                check("preparazione colloquio generata sul match del proprietario",
                      (r.status_code, r.json()["spunti"], len(r.json()["domande"]),
                       r.json()["lingua"], r.headers["cache-control"]),
                      (200, ["Spunto specifico in Italian"], 1, "Italian", "no-store"))
                primo_colloquio = r.json()
                r = client.post(f"/me/offerte/{match_stato['m2']}/colloquio",
                                params={"lingua": "it"}, headers=auth_header)
                check("seconda apertura del colloquio: cache senza rigenerazione",
                      (r.json(), len(chiamate_colloquio)), (primo_colloquio, 1))
                r = client.post(f"/me/offerte/{match_stato['m2']}/colloquio",
                                params={"lingua": "en"}, headers=auth_header)
                check("la preparazione colloquio ha cache separata per lingua",
                      (r.json()["lingua"], len(chiamate_colloquio)), ("English", 2))
                r = client.post(f"/me/offerte/{match_stato['m3']}/colloquio",
                                params={"lingua": "it"}, headers=auth_header)
                check("senza dati: nessuna preparazione colloquio generica",
                      r.status_code, 409)
            finally:
                colloquio_dettaglio.genera_colloquio = genera_colloquio_originale
                os.environ.pop("GLM_API_KEY", None)

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("UPDATE user_clusters SET is_paused = true WHERE id = %s",
                                (ricerche_stato["HR Manager"],))
                    cur.execute("UPDATE jobs SET status = 'expired', expired_at = now(), "
                                "purged_at = now() WHERE id = %s", (job_stato["m1"],))
                setup.commit()
            r = client.get("/me/offerte", params={
                "vista": "match", "ricerca_id": ricerche_stato["HR Manager"], "limite": 100},
                headers=auth_header)
            check("ricerca in pausa: nessun match corrente", r.json()["totale"], 0)
            r = client.get("/me/offerte", params={
                "vista": "azioni", "ricerca_id": ricerche_stato["HR Manager"], "limite": 100},
                headers=auth_header)
            check("pausa e scadenza non cancellano lo storico filtrato", r.json()["totale"], 7)
            r = client.get(f"/me/offerte/{match_stato['m1']}", headers=auth_header)
            check("dettaglio salvato dopo pausa e scadenza: sempre apribile",
                  (r.status_code, r.json()["id"], r.json()["offerta_status"],
                   r.json()["ricerca_eleggibile_ids"]),
                  (200, match_stato["m1"], "expired", []))
            check("associazioni correnti ancora distinte dopo la pausa",
                  set(r.json()["ricerca_ids"]), set(ricerche_stato.values()))

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("DELETE FROM user_clusters WHERE id = %s",
                                (ricerche_stato["HR Manager"],))
                setup.commit()
            check("ricerca rimossa non e' un filtro corrente valido",
                  client.get("/me/offerte", params={
                      "vista": "azioni", "ricerca_id": ricerche_stato["HR Manager"]},
                      headers=auth_header).status_code, 404)
            r = client.get("/me/offerte", params={"vista": "azioni", "limite": 100},
                           headers=auth_header)
            check("rimuovere la ricerca non rimuove le azioni dallo storico",
                  r.json()["totale"], 7)
            r = client.get("/me/offerte", params={
                "vista": "azioni", "ricerca_id": cluster_id, "limite": 100},
                headers=auth_header)
            check("vecchio cluster UUID accettato quando torna univoco",
                  (r.status_code, r.json()["totale"]), (200, 7))
            r = client.get(f"/me/offerte/{match_stato['m1']}", headers=auth_header)
            check("nessuna attribuzione storica inventata dopo la rimozione",
                  r.json()["ricerca_ids"], [ricerche_stato["HRBP"]])

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute("DELETE FROM matches WHERE user_id IN (%s,%s)",
                                (owner_id, other_id))
                    cur.execute("DELETE FROM user_clusters WHERE user_id IN (%s,%s)",
                                (owner_id, other_id))
                    cur.execute("DELETE FROM user_cvs WHERE user_id = %s", (owner_id,))
                    cur.execute("DELETE FROM jobs WHERE source_job_id LIKE 'api-state-%%'")
                    cur.execute("DELETE FROM users WHERE id = %s", (other_id,))
                setup.commit()

            with psycopg.connect(database_url()) as setup:
                with setup.cursor() as cur:
                    cur.execute(
                        "INSERT INTO jobs (source,source_job_id,url,canonical_url,domain_derived,"
                        "title,title_normalized,organization,date_posted,countries,raw,link_kind) "
                        "VALUES ('fantastic','api-state-tracker-only',"
                        "'https://acme.example/api-state-tracker-only',"
                        "'https://acme.example/api-state-tracker-only','acme.example',"
                        "'Tracker senza candidatura','tracker senza candidatura','Acme',now(),"
                        "ARRAY['IT'],'{}'::jsonb,'career_site') RETURNING id")
                    job_tracker_only = cur.fetchone()[0]
                    cur.execute(
                        "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model) "
                        "VALUES (%s,%s,88,'tracker only',70,'test') RETURNING id",
                        (owner_id, job_tracker_only))
                    match_tracker_only = str(cur.fetchone()[0])
                setup.commit()

            def ids_azioni_tracker():
                risposta = client.get("/me/offerte", params={
                    "vista": "azioni", "limite": 100}, headers=auth_header)
                return match_nel_json(risposta)

            r = client.put(f"/me/offerte/{match_tracker_only}/stato",
                           json=stato(0, note="Lavoro personale prima della candidatura"),
                           headers=auth_header)
            check("nota senza candidatura entra nello storico azioni",
                  (r.status_code, r.json()["candidatura"], r.json()["action_version"],
                   ids_azioni_tracker()), (200, "not_started", 1, {match_tracker_only}))
            check("la nota tracker-only sopravvive a un altro refresh",
                  ids_azioni_tracker(), {match_tracker_only})
            r = client.put(f"/me/offerte/{match_tracker_only}/stato",
                           json=stato(1, note=None, prossima_azione="Preparare il portfolio"),
                           headers=auth_header)
            check("prossima azione senza candidatura entra nello storico",
                  (r.status_code, r.json()["note"], ids_azioni_tracker()),
                  (200, None, {match_tracker_only}))
            r = client.put(f"/me/offerte/{match_tracker_only}/stato",
                           json=stato(2, prossima_azione=None), headers=auth_header)
            check("senza altro lavoro personale il match lascia lo storico",
                  (r.status_code, ids_azioni_tracker()), (200, set()))

            inizio_tracker = datetime.now(timezone.utc) + timedelta(days=3)
            r = client.post(f"/me/offerte/{match_tracker_only}/colloqui",
                            json={"inizio": inizio_tracker.isoformat(),
                                  "note": "Colloquio informale", "promemoria_minuti": 90},
                            headers=auth_header)
            check("colloquio senza candidatura entra nello storico",
                  (r.status_code, r.json()["action_version"], ids_azioni_tracker()),
                  (200, 4, {match_tracker_only}))
            colloquio_tracker = r.json()["colloqui"][0]
            check("POST colloquio usa i nomi del contratto app",
                  set(colloquio_tracker), {"id", "inizio", "note", "promemoria_minuti"})
            r = client.get(f"/me/offerte/{match_tracker_only}/colloqui",
                           headers=auth_header)
            check("GET colloqui usa gli stessi nomi del contratto app",
                  (r.status_code, set(r.json()["colloqui"][0])),
                  (200, {"id", "inizio", "note", "promemoria_minuti"}))
            r = client.delete(
                f"/me/offerte/{match_tracker_only}/colloqui/{colloquio_tracker['id']}",
                headers=auth_header)
            check("rimosso l'ultimo colloquio lo storico tracker-only si svuota",
                  (r.status_code, ids_azioni_tracker()), (200, set()))

            blob_tracker: dict[str, bytes] = {}
            salva_originale = app_module.storage.salva
            leggi_originale = app_module.storage.leggi
            elimina_originale = app_module.storage.elimina
            vecchia_kek = os.environ.get("CV_KEK")
            os.environ["CV_KEK"] = secrets.token_hex(32)
            app_module.storage.salva = lambda chiave, dati: blob_tracker.__setitem__(chiave, dati)
            app_module.storage.leggi = lambda chiave: blob_tracker[chiave]
            app_module.storage.elimina = lambda chiave: blob_tracker.pop(chiave, None)
            try:
                contenuto_tracker = b"%PDF-1.4\ntracker only"
                r = client.post(f"/me/offerte/{match_tracker_only}/documenti",
                                files={"file": ("tracker.pdf", contenuto_tracker,
                                                "application/pdf")},
                                headers=auth_header)
                check("documento senza candidatura entra nello storico",
                      (r.status_code, r.json()["action_version"], ids_azioni_tracker()),
                      (200, 6, {match_tracker_only}))
                documento_tracker = r.json()["documenti"][0]
                check("POST documento usa i nomi del contratto app",
                      set(documento_tracker), {"id", "nome", "mime", "dimensione", "creato_il"})
                r = client.get(f"/me/offerte/{match_tracker_only}/documenti",
                               headers=auth_header)
                check("GET documenti usa gli stessi nomi del contratto app",
                      (r.status_code, set(r.json()["documenti"][0])),
                      (200, {"id", "nome", "mime", "dimensione", "creato_il"}))
                r = client.get(
                    f"/me/offerte/{match_tracker_only}/documenti/{documento_tracker['id']}",
                    headers=auth_header)
                check("GET /documenti/{id} scarica i byte decifrati",
                      (r.status_code, r.content, r.headers["cache-control"]),
                      (200, contenuto_tracker, "no-store"))
                r = client.delete(
                    f"/me/offerte/{match_tracker_only}/documenti/{documento_tracker['id']}",
                    headers=auth_header)
                check("rimosso l'ultimo documento lo storico tracker-only si svuota",
                      (r.status_code, ids_azioni_tracker()), (200, set()))
                r = client.put(f"/me/offerte/{match_tracker_only}/stato",
                               json=stato(7, contatto="recruiter@example.com"),
                               headers=auth_header)
                check("contatto senza candidatura entra e resta nello storico",
                      (r.status_code, r.json()["contatto"], ids_azioni_tracker()),
                      (200, "recruiter@example.com", {match_tracker_only}))
                check("il contatto tracker-only sopravvive a un altro refresh",
                      ids_azioni_tracker(), {match_tracker_only})
                r = client.put(f"/me/offerte/{match_tracker_only}/stato",
                               json=stato(8, contatto=None), headers=auth_header)
                check("rimosso il contatto lo storico tracker-only si svuota",
                      (r.status_code, ids_azioni_tracker()), (200, set()))
            finally:
                app_module.storage.salva = salva_originale
                app_module.storage.leggi = leggi_originale
                app_module.storage.elimina = elimina_originale
                if vecchia_kek is None:
                    del os.environ["CV_KEK"]
                else:
                    os.environ["CV_KEK"] = vecchia_kek
                with psycopg.connect(database_url()) as setup:
                    with setup.cursor() as cur:
                        cur.execute("DELETE FROM matches WHERE id = %s", (match_tracker_only,))
                        cur.execute("DELETE FROM jobs WHERE id = %s", (job_tracker_only,))
                    setup.commit()

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

            print("\n— token push dell'app —")
            check("token push arbitrario rifiutato",
                  client.post("/me/push/token", headers=auth_header,
                              json={"token": "aaaaaaaaaa", "platform": "ios"}).status_code,
                  422)
            token_push = "ExponentPushToken[audit-main]"
            check("token Expo valido accettato",
                  client.post("/me/push/token", headers=auth_header,
                              json={"token": token_push, "platform": "ios"}).status_code,
                  204)
            check("registrazione push idempotente",
                  client.post("/me/push/token", headers=auth_header,
                              json={"token": token_push, "platform": "ios"}).status_code,
                  204)
            for i in range(9):
                client.post("/me/push/token", headers=auth_header,
                            json={"token": f"ExponentPushToken[audit-{i}]",
                                  "platform": "android"})
            check("l'undicesimo dispositivo e' rifiutato",
                  client.post("/me/push/token", headers=auth_header,
                              json={"token": "ExponentPushToken[audit-overflow]",
                                    "platform": "ios"}).status_code, 409)
            check("aggiornare un token gia' presente al limite resta consentito",
                  client.post("/me/push/token", headers=auth_header,
                              json={"token": token_push, "platform": "ios"}).status_code,
                  204)
            check("rimozione del token push",
                  client.request("DELETE", "/me/push/token", headers=auth_header,
                                 json={"token": token_push}).status_code, 204)
            check("il token rimosso non resta nel database",
                  seen_by_other("SELECT count(*) FROM user_push_tokens "
                                "WHERE token = %s", (token_push,)), 0)

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
                **app_module.cv.estrai_localita(testo),
                "raw_extraction": {"note": "finto"},
            }

            try:
                contenuto = (b"Rome, Italy - Open to Relocate\n"
                             b"Curriculum di prova: HR Business Partner con 8 anni.")
                r = client.post("/me/cv", files={"file": ("cv.txt", contenuto, "text/plain")},
                                headers=auth_header)
                check("upload del CV: 200 col profilo proposto", r.status_code, 200)
                profilo = r.json().get("profilo", {})
                check("il profilo arriva per la precompilazione",
                      (profilo.get("families"), profilo.get("seniority")),
                      (["Human Resources"], "5-10"))
                check("upload: località strutturata e trasferimento",
                      (profilo.get("location_city"), profilo.get("location_country"),
                       profilo.get("relocation_open")), ("Rome", "IT", True))
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

                cv_attivo_prima = seen_by_other(
                    "SELECT id FROM user_cvs WHERE user_id = ("
                    "  SELECT id FROM users WHERE email = 'pref@test.dev') "
                    "  AND status = 'active'")
                analisi_valida = app_module._analizza_cv
                try:
                    app_module._analizza_cv = lambda conn, testo: {
                        "families": [], "seniority": None, "skills": [],
                        "languages": ["Italian"], "years_experience": None,
                        "raw_extraction": {"roles": [], "certifications": [], "education": []},
                        **app_module.cv.estrai_localita(testo),
                    }
                    r = client.post("/me/cv", files={"file": (
                        "bolletta-luce.txt",
                        b"BOLLETTA LUCE: consumi, tariffa e importo da pagare entro il 30 novembre.",
                        "text/plain")}, headers=auth_header)
                    check("una bolletta non diventa un CV e lascia tutto invariato",
                          (r.status_code, r.json()["detail"], len(salvati),
                           seen_by_other(
                               "SELECT id FROM user_cvs WHERE user_id = ("
                               "  SELECT id FROM users WHERE email = 'pref@test.dev') "
                               "  AND status = 'active'")),
                          (422, "cv_non_professionale", 1, cv_attivo_prima))
                    r = client.post("/me/cv", files={"file": (
                        "vuoto.txt", b"", "text/plain")}, headers=auth_header)
                    check("un documento vuoto ha un errore specifico e non crea blob",
                          (r.status_code, r.json()["detail"], len(salvati)),
                          (422, "cv_testo_insufficiente", 1))
                finally:
                    app_module._analizza_cv = analisi_valida

                with psycopg.connect(database_url()) as setup:
                    with setup.cursor() as cur:
                        cur.execute("SELECT id FROM users WHERE email = 'pref@test.dev'")
                        cv_owner = cur.fetchone()[0]
                        for chiave, salvato in (("saved", True), ("pristine-1", False)):
                            sid = f"api-cv-{chiave}"
                            url = f"https://acme.example/{sid}"
                            cur.execute(
                                "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,"
                                "title_normalized,organization,date_posted,raw,link_kind) VALUES "
                                "('fantastic',%s,%s,%s,'CV match','cv match','Acme',now(),"
                                "'{}'::jsonb,'career_site') RETURNING id", (sid, url, url))
                            jid = cur.fetchone()[0]
                            cur.execute("INSERT INTO job_clusters (job_id,cluster_id) "
                                        "VALUES (%s,%s)", (jid, cluster_id))
                            cur.execute(
                                "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,"
                                "model,saved_at,action_version,user_action_at) VALUES "
                                "(%s,%s,90,'cv',70,'test',CASE WHEN %s THEN now() ELSE NULL END,"
                                "%s,CASE WHEN %s THEN now() ELSE NULL END) RETURNING id",
                                (cv_owner, jid, salvato, 1 if salvato else 0, salvato))
                            if salvato:
                                match_cv_salvato = str(cur.fetchone()[0])
                    setup.commit()

                secondo = b"Secondo curriculum, aggiornato: Chief People Officer."
                r = client.post("/me/cv", files={"file": ("cv2.txt", secondo, "text/plain")},
                                headers=auth_header)
                check("il secondo upload va a buon fine", r.status_code, 200)
                check("nuovo CV: riaperto solo il match senza azioni",
                      r.json()["match_riaperti"], 1)
                check("nuovo CV: il match salvato conserva id e feedback",
                      seen_by_other("SELECT saved_at IS NOT NULL AND action_version = 1 "
                                    "FROM matches WHERE id = %s", (match_cv_salvato,)), True)
                check("nuovo CV: il match vergine e' stato eliminato",
                      seen_by_other("SELECT count(*) FROM matches m JOIN jobs j ON j.id = m.job_id "
                                    "WHERE j.source_job_id = 'api-cv-pristine-1'"), 0)
                check("il primo CV è degradato a superseded: uno attivo solo",
                      seen_by_other("SELECT count(*) FROM user_cvs WHERE user_id = ("
                                    "  SELECT id FROM users WHERE email = 'pref@test.dev') "
                                    "  AND status = 'active'"), 1)
                check("il vecchio file cifrato è stato eliminato dal bucket",
                      len(salvati), 1)

                r = client.post("/me/cv", files={"file": ("cv.txt", b" corto ", "text/plain")},
                                headers=auth_header)
                check("un file senza testo sufficiente è rifiutato", r.status_code, 422)

                # Il CV si rivede: e' il suo file, la sessione e' l'unica
                # chiave e la query filtra per user_id — non c'e' un id da
                # indovinare. Le intestazioni sono meta' della funzione,
                # quindi si provano una per una. `storage.leggi` va
                # sostituito come le altre: qui non si parla col bucket vero.
                app_module.storage.leggi = lambda chiave: salvati[chiave]
                r = client.get("/me/cv/file", headers=auth_header)
                check("il proprietario rivede il suo CV", r.status_code, 200)
                check("e i byte sono quelli in chiaro del CV ATTIVO "
                      "(il secondo: il primo e' superseded)",
                      secondo in r.content, True)
                check("no-store: un CV in cache e' un CV altrove",
                      "no-store" in r.headers.get("cache-control", ""), True)
                check("nosniff", r.headers.get("x-content-type-options"), "nosniff")
                check("sandbox in CSP",
                      "sandbox" in r.headers.get("content-security-policy", ""), True)
                check("mostrato, non scaricato",
                      r.headers.get("content-disposition", "").startswith("inline"),
                      True)
                check("senza sessione: 401",
                      client.get("/me/cv/file").status_code, 401)

                # Il profilo si CORREGGE: l'onboarding lo lasciava
                # modificare e buttava via tutto, perche' la rotta non
                # esisteva. Vale la regola dell'estrazione — fuori
                # vocabolario si scarta, non si corregge.
                with psycopg.connect(database_url()) as setup:
                    with setup.cursor() as cur:
                        cur.execute(
                            "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,"
                            "title_normalized,organization,date_posted,raw,link_kind) VALUES "
                            "('fantastic','api-cv-pristine-2','https://acme.example/api-cv-pristine-2',"
                            "'https://acme.example/api-cv-pristine-2','CV match 2','cv match 2',"
                            "'Acme',now(),'{}'::jsonb,'career_site') RETURNING id")
                        jid = cur.fetchone()[0]
                        cur.execute("INSERT INTO job_clusters (job_id,cluster_id) VALUES (%s,%s)",
                                    (jid, cluster_id))
                        cur.execute(
                            "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model) "
                            "VALUES (%s,%s,88,'da riaprire',70,'test')", (cv_owner, jid))
                    setup.commit()
                r = client.put("/me/cv/profilo", headers=auth_header, json={
                    "families": ["Human Resources", "Piccionaia"],
                    "seniority": "10+", "skills": ["  Payroll  ", ""],
                    "languages": ["Italian", "Klingon"],
                    "years_experience": 999,
                    "location_city": "  Rome  ", "location_country": "IT",
                    "relocation_open": True,
                })
                check("correggere il profilo: 200", r.status_code, 200)
                d = r.json()
                check("correzione profilo: riaperto solo il match vergine",
                      d["riaperti"], 1)
                check("correzione profilo: il match salvato non sparisce",
                      seen_by_other("SELECT saved_at IS NOT NULL FROM matches WHERE id = %s",
                                    (match_cv_salvato,)), True)
                check("correzione profilo: il nuovo match vergine e' eliminato",
                      seen_by_other("SELECT count(*) FROM matches m JOIN jobs j ON j.id = m.job_id "
                                    "WHERE j.source_job_id = 'api-cv-pristine-2'"), 0)
                check("le famiglie fuori catalogo si scartano",
                      d["families"], ["Human Resources"])
                check("le lingue fuori vocabolario pure", d["languages"], ["Italian"])
                check("la seniority valida passa", d["seniority"], "10+")
                check("le competenze si ripuliscono", d["skills"], ["Payroll"])
                check("gli anni si limitano a 70", d["years_experience"], 70)
                check("correzione profilo: località e trasferimento nella risposta",
                      (d["location_city"], d["location_country"], d["relocation_open"]),
                      ("Rome", "IT", True))
                check("GET /me/cv rilegge località e trasferimento",
                      tuple(client.get("/me/cv", headers=auth_header).json()[chiave]
                            for chiave in ("location_city", "location_country", "relocation_open")),
                      ("Rome", "IT", True))
                check("e il database ha la correzione, non la lettura di GLM",
                      seen_by_other("SELECT seniority FROM user_cvs "
                                    "WHERE user_id = (SELECT id FROM users "
                                    "  WHERE email = 'pref@test.dev') "
                                    "AND status = 'active'"), "10+")
                check("senza nemmeno una famiglia valida: 422",
                      client.put("/me/cv/profilo", headers=auth_header,
                                 json={"families": ["Piccionaia"]}).status_code, 422)
                documento = Document()
                documento.add_paragraph(
                    "HR Business Partner con otto anni di esperienza internazionale.")
                tabella = documento.add_table(rows=1, cols=1)
                tabella.cell(0, 0).text = "Employee relations"
                buffer_docx = io.BytesIO()
                documento.save(buffer_docx)
                r = client.post(
                    "/me/cv", headers=auth_header,
                    files={"file": (
                        "Życiorys.docx", buffer_docx.getvalue(),
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
                check("DOCX reale accettato", r.status_code, 200)
                r = client.get("/me/cv/file", headers=auth_header)
                check("filename Unicode non rompe il download", r.status_code, 200)
                check("filename UTF-8 e fallback ASCII presenti",
                      ("filename*=" in r.headers.get("content-disposition", "")
                       and "%C5%BB" in r.headers["content-disposition"]), True)

                grande = b"a" * (20 * 1024 * 1024)
                r = client.post("/me/cv", headers=auth_header,
                                files={"file": ("grande.txt", grande, "text/plain")})
                check("20 MiB esatti accettati", r.status_code, 200)
                r = client.post("/me/cv", headers=auth_header,
                                files={"file": ("oltre.txt", grande + b"a", "text/plain")})
                check("20 MiB + 1 byte rifiutato con 413", r.status_code, 413)
                r = client.post("/me/cv", headers=auth_header,
                                files={"file": ("rotto.pdf", b"%PDF-1.4\nnon valido",
                                                "application/pdf")})
                check("PDF malformato: 422 e non 500", r.status_code, 422)
            finally:
                app_module.storage.salva = salva_originale
                app_module.storage.elimina = elimina_originale
                app_module._analizza_cv = analizza_originale
                del os.environ["CV_KEK"]
                with psycopg.connect(database_url()) as setup:
                    with setup.cursor() as cur:
                        cur.execute("DELETE FROM matches WHERE user_id = ("
                                    "SELECT id FROM users WHERE email = 'pref@test.dev')")
                        cur.execute("DELETE FROM jobs WHERE source_job_id LIKE 'api-cv-%%'")
                    setup.commit()

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
            print("\n— perimetro e identita Mavult —")
            ambiente = {k: os.environ.get(k) for k in
                        ("PRODUCT_NAME", "SERVICE_NAME", "B2B_ENABLED", "CORS_ORIGINS",
                         "DEFAULT_DELIVERY_CHANNELS")}
            try:
                os.environ.update(PRODUCT_NAME="Mavult", SERVICE_NAME="mavult-api",
                                  B2B_ENABLED="0", CORS_ORIGINS="https://mavult.com",
                                  DEFAULT_DELIVERY_CHANNELS="app")
                with TestClient(create_app()) as mavult:
                    r = mavult.get("/")
                    check("root Mavult attiva", (r.status_code, r.json()),
                          (200, {"servizio": "mavult-api", "stato": "ok"}))
                    check("demo B2B chiusa su Mavult: 404, non 500",
                          mavult.get("/v1/demo/stats").status_code, 404)
                    check("booking B2B chiuso su Mavult",
                          mavult.get("/booking/slots").status_code, 404)
                    check("portale B2B chiuso su Mavult",
                          mavult.post("/portale/cerca", json={}).status_code, 404)
                    check("le rotte B2C restano esposte",
                          mavult.get("/me").status_code, 401)
                    pre = mavult.options(
                        "/auth/magic-link", headers={
                            "Origin": "https://mavult.com",
                            "Access-Control-Request-Method": "POST"})
                    check("CORS Mavult", pre.headers.get("access-control-allow-origin"),
                          "https://mavult.com")
                    r = mavult.post("/auth/magic-link",
                                    json={"email": "mavult-default@test.dev", "locale": "it"})
                    check("nuovo account Mavult con canale app",
                          (r.status_code,
                           seen_by_other("SELECT delivery_channels FROM users "
                                         "WHERE email='mavult-default@test.dev'")),
                          (202, ["app"]))
                    check("magic link Mavult anche nel contenuto",
                          ("Mavult" in contenuti_email[-1][0]
                           and "M A V U L T" in contenuti_email[-1][2]), True)
                check("il benvenuto usa il marchio Mavult",
                      "<b>Mavult</b>" in app_module._benvenuto(
                          "en", None, "daily", "telegram"), True)
            finally:
                for chiave, valore in ambiente.items():
                    if valore is None:
                        os.environ.pop(chiave, None)
                    else:
                        os.environ[chiave] = valore
    finally:
        auth.richiedi_magic_link = originale
        app_module.ponte_ats.importa = importa_originale

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
