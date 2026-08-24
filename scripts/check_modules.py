#!/usr/bin/env python3
"""Verifica dei moduli Python che pilotano le funzioni SQL.

    python scripts/check_modules.py

check_constraints.py prova le funzioni SQL da sole, dentro una transazione
annullata. Resta scoperto lo strato sopra — nivult.retention e nivult.gdpr — ed
è proprio lì che si era nascosto il bug dei savepoint: i lotti sembravano
eseguiti e non venivano committati.

Due tecniche, qui:

  - le asserzioni "ha davvero committato" leggono da una SECONDA connessione.
    Una verifica sulla stessa connessione avrebbe visto il lavoro non committato
    e sarebbe passata lo stesso, che è esattamente come il bug era sfuggito;
  - il caso che mi aveva ingannato ha un test suo: chiamare i moduli con una
    transazione già aperta deve fallire in modo esplicito.

DISTRUTTIVO: committa e poi ripulisce. Solo su database _test/_dev.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psycopg  # noqa: E402

from nivult.config import database_name, database_url, safe_dsn  # noqa: E402
from nivult.gdpr import execute_deletion, request_deletion  # noqa: E402
from nivult.matching import worker  # noqa: E402
from nivult.retention import purge  # noqa: E402

from datetime import datetime, timezone  # noqa: E402

VEC = "[" + ",".join(["0.01"] * 1024) + "]"
PASSED: list[str] = []
FAILED: list[str] = []

DATA_TABLES = ["users", "clusters", "jobs", "deletion_requests", "api_usage"]


def ok(label: str, detail: str = "") -> None:
    PASSED.append(label)
    print(f"  ok    {label}{('  -> ' + detail) if detail else ''}")


def bad(label: str, detail: str) -> None:
    FAILED.append(label)
    print(f"  FAIL  {label} — {detail}")


def check(label: str, got, expected) -> None:
    if got == expected:
        ok(label, repr(got))
    else:
        bad(label, f"atteso {expected!r}, ottenuto {got!r}")


def section(title: str) -> None:
    print(f"\n{title}")


def expect_refusal(label: str, fn) -> None:
    """Deve sollevare RuntimeError, non degradare in silenzio."""
    try:
        fn()
    except RuntimeError as exc:
        if "transazione" in str(exc):
            ok(label, "RuntimeError esplicito")
        else:
            bad(label, f"RuntimeError ma con messaggio inatteso: {exc}")
        return
    except Exception as exc:  # noqa: BLE001
        bad(label, f"eccezione sbagliata: {type(exc).__name__}: {exc}")
        return
    bad(label, "NON ha sollevato: degraderebbe in silenzio a savepoint")


def wipe(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(DATA_TABLES)} CASCADE")
    conn.commit()


def seed(conn: psycopg.Connection) -> dict:
    """Un cluster, un utente completo di storico, offerte vive e morte."""
    ids: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO clusters (family,country,daily_credit_cap) "
                    "VALUES ('Human Resources','IT',100) RETURNING id")
        ids["cluster"] = cur.fetchone()[0]

        cur.execute("INSERT INTO users (email,plan,subscription_status,delivery_channel,"
                    "frequency,timezone) VALUES ('mod@example.test','pro','active','email',"
                    "'daily','Europe/Rome') RETURNING id")
        ids["user"] = cur.fetchone()[0]

        cur.execute("INSERT INTO user_cvs (user_id,storage_key,embedding,embedding_model,"
                    "encryption_algo, encrypted_dek, nonce, auth_tag, kek_version) "
                    "VALUES (%s,'cv/mod.pdf',%s,'bge-m3','aes-256-gcm', decode(repeat('ab',32),'hex'), decode(repeat('cd',12),'hex'), decode(repeat('ef',16),'hex'), 1)", (ids["user"], VEC))
        cur.execute("INSERT INTO user_clusters (user_id,cluster_id) VALUES (%s,%s)",
                    (ids["user"], ids["cluster"]))
        cur.execute("INSERT INTO api_usage (provider,operation,user_id,cost_micros) "
                    "VALUES ('glm','score',%s,4200)", (ids["user"],))
        cur.execute("INSERT INTO login_tokens (user_id,token_hash,expires_at,requested_ip) "
                    "VALUES (%s, repeat('a',64), now() + interval '15 min','203.0.113.7')",
                    (ids["user"],))
        cur.execute("INSERT INTO sessions (user_id,token_hash,expires_at,origin,ip) "
                    "VALUES (%s, repeat('b',64), now() + interval '30 days',"
                    "'magic_link','203.0.113.7')", (ids["user"],))
        cur.execute("INSERT INTO oauth_identities (provider,subject,user_id,email_at_link) "
                    "VALUES ('google','sub-mod',%s,'mod@example.test')", (ids["user"],))

        def mkjob(sid, status, expired_days, posted_days):
            cur.execute(
                "INSERT INTO jobs (source,source_job_id,url,canonical_url,domain_derived,"
                "title,title_normalized,organization,date_posted,countries,ai_key_skills,"
                "status,expired_at,raw,link_kind) VALUES ('fantastic',%s,%s,%s,'acme.example',"
                "%s,'hr manager','Acme SpA', now() - make_interval(days => %s::int),"
                "ARRAY['IT'],ARRAY['recruiting'],%s,"
                "CASE WHEN %s::int IS NULL THEN NULL "
                "     ELSE now() - make_interval(days => %s::int) END,"
                "jsonb_build_object('descr', repeat('x', 2000)), 'career_site') RETURNING id",
                (sid, f"https://acme.example/careers/{sid}",
                 f"https://acme.example/careers/{sid}", f"HR Manager {sid}",
                 posted_days, status, expired_days, expired_days))
            jid = cur.fetchone()[0]
            cur.execute("INSERT INTO job_clusters (job_id,cluster_id) VALUES (%s,%s)",
                        (jid, ids["cluster"]))
            cur.execute("INSERT INTO job_embeddings (job_id,embedding,model) "
                        "VALUES (%s,%s,'bge-m3')", (jid, VEC))
            return jid

        ids["job_free"] = mkjob("free", "expired", 90, 150)     # morta, mai valutata
        ids["job_sent"] = mkjob("sent", "expired", 90, 150)     # morta, ma inviata
        ids["job_live"] = mkjob("live", "active", None, 900)    # viva e vecchissima

        cur.execute("INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model) "
                    "VALUES (%s,%s,93,'Profilo allineato',80,'glm-5.2') RETURNING id",
                    (ids["user"], ids["job_sent"]))
        ids["match"] = cur.fetchone()[0]

        cur.execute("INSERT INTO digests (user_id,channel,scheduled_for,status,sent_at,"
                    "jobs_evaluated_count,jobs_sent_count) VALUES (%s,'email',"
                    "now() - interval '100 days','sent', now() - interval '100 days',15,1) "
                    "RETURNING id", (ids["user"],))
        ids["digest"] = cur.fetchone()[0]
        cur.execute("INSERT INTO digest_items (digest_id,job_id,user_id,match_id,rank,"
                    "score_snapshot,reason_snapshot) VALUES (%s,%s,%s,%s,1,93,'Profilo allineato')",
                    (ids["digest"], ids["job_sent"], ids["user"], ids["match"]))
    conn.commit()
    return ids


def seed_digest(conn: psycopg.Connection) -> dict[str, str]:
    """Due utenti dovuti, un cluster, e offerte che coprono ogni ramo del funnel."""
    ids: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute("INSERT INTO clusters (family, country) "
                    "VALUES ('Human Resources','IT') RETURNING id")
        ids["cluster"] = cur.fetchone()[0]

        def utente(email, next_min_ago=60):
            cur.execute(
                "INSERT INTO users (email, plan, subscription_status, delivery_channel, "
                "  frequency, timezone, next_digest_at) VALUES "
                "(%s,'pro','active','email','daily','Europe/Rome', now() - make_interval(mins => %s::int)) "
                "RETURNING id", (email, next_min_ago))
            uid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_cvs (user_id, storage_key, families, seniority, skills, "
                "  languages, years_experience, encryption_algo, encrypted_dek, nonce, "
                "  auth_tag, kek_version) VALUES "
                "(%s,'cv/digest.pdf', ARRAY['Human Resources'],'5-10', ARRAY['recruiting'], "
                "'[\"Italian\"]'::jsonb, 8, 'aes-256-gcm', decode(repeat('ab',32),'hex'), "
                "decode(repeat('cd',12),'hex'), decode(repeat('ef',16),'hex'), 1) RETURNING id",
                (uid,))
            ids["cv_" + email] = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_clusters (user_id, cluster_id, languages, min_seniority, "
                "  max_seniority, employment_types) VALUES "
                "(%s,%s, ARRAY['Italian'], '2-5', '10+', ARRAY['FULL_TIME'])",
                (uid, ids["cluster"]))
            return uid

        ids["a"] = utente("digest-a@example.test")
        ids["b"] = utente("digest-b@example.test", next_min_ago=30)

        def offerta(sid, *, lingua="Italian", esp="5-10", tipo="FULL_TIME",
                    link="career_site", giorni=1):
            cur.execute(
                "INSERT INTO jobs (source, source_job_id, url, canonical_url, "
                "  domain_derived, title, title_normalized, organization, date_posted, "
                "  countries, ai_job_language, ai_experience_level, ai_employment_type, "
                "  ai_key_skills, raw, link_kind, org_headcount) VALUES "
                "('fantastic', %s, %s, %s, 'acme.example', %s, %s, 'Acme SpA', "
                "  now() - make_interval(days => %s::int), ARRAY['IT'], %s, %s, %s, "
                "  ARRAY['recruiting'], '{}'::jsonb, %s, 500) RETURNING id",
                (sid, f"https://acme.example/{sid}", f"https://acme.example/{sid}",
                 f"Offerta {sid}", f"offerta {sid}", giorni, lingua, esp, tipo, link))
            jid = cur.fetchone()[0]
            cur.execute("INSERT INTO job_clusters (job_id, cluster_id) VALUES (%s,%s)",
                        (jid, ids["cluster"]))

        offerta("ok1")                                   # sopra soglia, diretta
        offerta("ok2", link="national_agency")           # 85, agenzia pubblica
        offerta("bassa")                                 # sotto soglia
        offerta("nulli", lingua=None, esp=None, tipo=None)  # tutti NULL: passa
        offerta("francese", lingua="French")             # filtrata dalla lingua
        offerta("junior", esp="0-2")                     # filtrata dalla seniority

        # B ha il budget del mese già consumato: il breaker deve dire di no.
        cur.execute(
            "INSERT INTO user_evaluation_budget (user_id, period_month, evaluations_used) "
            "VALUES (%s, date_trunc('month', current_date)::date, 5000)", (ids["b"],))
    conn.commit()
    return ids


class ValutatoreFinto:
    """Il contratto del valutatore, con punteggi decisi a tavolino."""

    def __init__(self, punteggi: dict[str, int]):
        self.punteggi = punteggi
        self.totale = {"input": 0, "cached": 0, "output": 0, "chiamate": 0}

    def valuta(self, profilo_testo, offerta):
        sid = offerta["source_job_id"]
        self.totale["chiamate"] += 1
        self.totale["input"] += 100
        self.totale["output"] += 10
        return self.punteggi.get(sid, 10), f"motivo breve {sid}", {"input": 100, "output": 10}

    def motiva(self, profilo_testo, offerta):
        sid = offerta["source_job_id"]
        self.totale["chiamate"] += 1
        return f"motivazione piena {sid}", {"input": 80, "output": 20}


def main() -> int:
    db = database_name()
    if not (db.endswith(("_test", "_dev")) or os.environ.get("NIVULT_ALLOW_DESTRUCTIVE") == "1"):
        print(f"rifiuto di girare su '{db}': serve un database _test/_dev.")
        return 2

    logging.basicConfig(level=logging.WARNING, format="%(levelname)-7s %(message)s")
    print(f"verifica moduli — {safe_dsn(database_url())}")

    work = psycopg.connect(database_url())  # connessione dei moduli
    # Testimone in autocommit: ogni lettura è una transazione a sé. Senza
    # autocommit resterebbe "idle in transaction" trattenendo lock, e il
    # TRUNCATE di pulizia lo aspetterebbe per sempre.
    other = psycopg.connect(database_url(), autocommit=True)

    def seen_by_other(sql: str, params=()) -> object:
        """Legge da un'altra connessione: passa solo se il lavoro è committato."""
        with other.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchone()[0]

    try:
        wipe(work)
        ids = seed(work)

        section("guard sulle transazioni (il caso che mi aveva ingannato)")
        dirty = psycopg.connect(database_url())
        with dirty.cursor() as cur:
            cur.execute("SELECT 1")  # da qui la connessione è INTRANS
        expect_refusal("purge() rifiuta una connessione con transazione aperta",
                       lambda: purge(dirty, older_than_days=60))
        expect_refusal("request_deletion() la rifiuta",
                       lambda: request_deletion(dirty, ids["user"]))
        expect_refusal("execute_deletion() la rifiuta",
                       lambda: execute_deletion(dirty, ids["user"]))
        check("nessuna offerta toccata dai tentativi rifiutati",
              seen_by_other("SELECT count(*) FROM jobs WHERE purged_at IS NOT NULL"), 0)
        dirty.rollback()
        dirty.close()

        section("retention.purge")
        dry = purge(work, older_than_days=60, dry_run=True)
        check("dry-run conta senza toccare: 1 da cancellare",
              dry["da_cancellare"], 1)
        check("dry-run conta senza toccare: 1 da svuotare",
              dry["da_svuotare"], 1)
        check("dry-run non ha modificato nulla",
              seen_by_other("SELECT count(*) FROM jobs"), 3)

        totals = purge(work, older_than_days=60, batch_size=100)
        check("purge: 1 cancellata", totals["cancellate"], 1)
        check("purge: 1 svuotata", totals["svuotate"], 1)
        # La prova che conta: lo vede un'ALTRA connessione, quindi è committato.
        check("COMMITTATO: la cancellazione è visibile da un'altra connessione",
              seen_by_other("SELECT count(*) FROM jobs WHERE id = %s", (ids["job_free"],)), 0)
        check("COMMITTATO: la lapide è visibile da un'altra connessione",
              seen_by_other("SELECT raw IS NULL AND purged_at IS NOT NULL FROM jobs "
                            "WHERE id = %s", (ids["job_sent"],)), True)
        check("l'offerta attiva da 900 giorni non è stata toccata",
              seen_by_other("SELECT raw IS NOT NULL FROM jobs WHERE id = %s",
                            (ids["job_live"],)), True)
        check("aggregati scritti e committati",
              seen_by_other("SELECT jobs_purged || '/' || jobs_tombstoned "
                            "FROM cluster_month_stats WHERE cluster_id = %s",
                            (ids["cluster"],)), "1/1")

        section("digest storico dopo lo svuotamento")
        # Se questi campi sparissero dalla lapide, un vecchio digest diventerebbe
        # illeggibile: resterebbero punteggio e motivazione senza sapere di quale
        # offerta. Il test blocca la lista di colonne che purge_dead_jobs azzera.
        row = seen_by_other(
            "SELECT j.title || ' | ' || j.organization || ' | ' || j.url "
            "FROM digest_items di JOIN jobs j ON j.id = di.job_id "
            "WHERE di.digest_id = %s", (ids["digest"],))
        check("titolo, azienda e link ancora leggibili dal vecchio digest",
              row, "HR Manager sent | Acme SpA | https://acme.example/careers/sent")
        check("motivazione e punteggio conservati nello snapshot",
              seen_by_other("SELECT score_snapshot || ' ' || reason_snapshot "
                            "FROM digest_items WHERE digest_id = %s", (ids["digest"],)),
              "93 Profilo allineato")
        check("la lapide ha però perso il peso: niente embedding",
              seen_by_other("SELECT count(*) FROM job_embeddings WHERE job_id = %s",
                            (ids["job_sent"],)), 0)

        section("GDPR end-to-end")
        req = request_deletion(work, ids["user"])
        check("COMMITTATO: utente marcato deleted, visibile da fuori",
              seen_by_other("SELECT status = 'deleted' AND deleted_at IS NOT NULL "
                            "AND next_digest_at IS NULL FROM users WHERE id = %s",
                            (ids["user"],)), True)
        check("richiesta registrata con la chiave del CV da rimuovere",
              seen_by_other("SELECT jsonb_array_length(pending_storage_keys) "
                            "FROM deletion_requests WHERE id = %s", (req,)), 1)
        check("seconda richiesta: ON CONFLICT tiene la prima",
              request_deletion(work, ids["user"]), req)
        check("una sola richiesta aperta",
              seen_by_other("SELECT count(*) FROM deletion_requests WHERE user_id = %s",
                            (ids["user"],)), 1)

        totals = execute_deletion(work, req, batch_size=100)
        check("cancellati i dati personali attesi",
              sorted(totals), ["api_usage:anonimizzate", "digest_items", "digests",
                               "login_tokens", "matches", "oauth_identities", "sessions",
                               "user_clusters", "user_cvs", "users"])
        for table in ("user_cvs", "user_clusters", "matches", "digest_items", "digests",
                      "login_tokens", "sessions", "oauth_identities"):
            check(f"COMMITTATO: {table} svuotata per l'utente",
                  seen_by_other(f"SELECT count(*) FROM {table} WHERE user_id = %s",
                                (ids["user"],)), 0)
        check("COMMITTATO: l'utente non esiste più",
              seen_by_other("SELECT count(*) FROM users WHERE id = %s", (ids["user"],)), 0)
        check("il costo sopravvive, senza collegamento alla persona",
              seen_by_other("SELECT count(*) FROM api_usage "
                            "WHERE user_id IS NULL AND cost_micros = 4200"), 1)
        check("la richiesta resta aperta: il file su storage non è stato rimosso",
              seen_by_other("SELECT status FROM deletion_requests WHERE id = %s", (req,)),
              "pending")
        check("le offerte NON sono dati personali: la lapide resta",
              seen_by_other("SELECT count(*) FROM jobs WHERE id = %s", (ids["job_sent"],)), 1)

        # Riprendibile: rilanciarla su un utente già svuotato non deve esplodere.
        again = execute_deletion(work, req, batch_size=100)
        check("execute_deletion è riprendibile senza errori", again, {"users": 0})

        section("digest end-to-end (valutatore finto)")
        wipe(work)
        dg = seed_digest(work)
        finto = ValutatoreFinto({"ok1": 90, "ok2": 85, "nulli": 85, "bassa": 40})
        adesso = datetime.now(timezone.utc)

        from psycopg.rows import dict_row
        with work.cursor(row_factory=dict_row) as cur:
            dovuti = worker.utenti_dovuti(cur, adesso)
        per_email = {u.email: u for u in dovuti}
        check("due utenti dovuti trovati", sorted(per_email), [
            "digest-a@example.test", "digest-b@example.test"])

        e = worker.digest_utente(work, per_email["digest-a@example.test"],
                                 dry_run=True, evaluatore=finto)
        check("digest A inviato (dry run)", e["stato"], "sent")
        check("digest A: 4 valutate, 3 inviate", (e["valutate"], e["inviate"]), (4, 3))
        check("COMMITTATO: digest sent visibile da fuori",
              seen_by_other("SELECT status FROM digests WHERE user_id = %s", (dg["a"],)), "sent")
        check("COMMITTATO: 4 match scritti, scarti compresi",
              seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s", (dg["a"],)), 4)
        check("l'offerta sotto soglia è registrata come non passata",
              seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s AND NOT passed",
                            (dg["a"],)), 1)
        check("i filtri deterministici hanno escluso 2 offerte SENZA valutarle",
              seen_by_other("SELECT count(*) FROM matches m JOIN jobs j ON j.id = m.job_id "
                            "WHERE m.user_id = %s AND j.source_job_id IN ('francese','junior')",
                            (dg["a"],)), 0)
        check("ordine per punteggio e poi trasparenza del link",
              seen_by_other("SELECT string_agg(j.source_job_id || ':' || di.rank, ',' "
                            "  ORDER BY di.rank) FROM digest_items di "
                            "  JOIN jobs j ON j.id = di.job_id WHERE di.user_id = %s",
                            (dg["a"],)), "ok1:1,nulli:2,ok2:3")
        check("il digest mostra la motivazione piena, non quella breve",
              seen_by_other("SELECT reason_snapshot FROM digest_items di "
                            "  JOIN jobs j ON j.id = di.job_id "
                            " WHERE di.user_id = %s AND di.rank = 1", (dg["a"],)),
              "motivazione piena ok1")
        check("il costo è quello calcolato: 4 valutazioni + 3 motivazioni",
              finto.totale["chiamate"], 4 + 3)
        check("rischedulato all'orario locale dell'utente (8 Roma = 6 UTC)",
              seen_by_other("SELECT (next_digest_at AT TIME ZONE 'UTC')::time = '06:00' "
                            " AND next_digest_at > now() FROM users WHERE id = %s",
                            (dg["a"],)), True)

        # Idempotenza dello slot: rilanciare sullo stesso slot non fa nulla.
        di_nuovo = worker.digest_utente(work, per_email["digest-a@example.test"],
                                        dry_run=True, evaluatore=finto)
        check("lo slot già consegnato non si ripete", di_nuovo["stato"], "già consegnato")

        # Slot successivo: niente offerte nuove -> skipped_empty, non un fallimento.
        with work.cursor() as cur:
            cur.execute("UPDATE users SET next_digest_at = now() - interval '30 min' "
                        "WHERE id = %s", (dg["a"],))
        work.commit()
        with work.cursor(row_factory=dict_row) as cur:
            u2 = next(u for u in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                      if u.email == "digest-a@example.test")
        e2 = worker.digest_utente(work, u2, dry_run=True, evaluatore=finto)
        check("senza offerte nuove il digest è skipped_empty", e2["stato"], "skipped_empty")
        check("l'anti-ripetizione non ha rivalutato nulla",
              seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s", (dg["a"],)), 4)
        check("un digest vuoto è un esito legittimo, distinto dal fallimento",
              seen_by_other("SELECT count(*) FROM digests WHERE user_id = %s "
                            "  AND status = 'skipped_empty'", (dg["a"],)), 1)

        # Budget esaurito: il breaker per utente, non un costo fuori controllo.
        eb = worker.digest_utente(work, per_email["digest-b@example.test"],
                                  dry_run=True, evaluatore=finto)
        check("budget esaurito: digest fallito con motivo esplicito", eb["stato"], "failed_budget")
        check("il messaggio dice piano e consumato",
              seen_by_other("SELECT error_message FROM digests WHERE user_id = %s "
                            "  AND status = 'failed'", (dg["b"],)),
              "budget di valutazione esaurito (5000/5000, piano pro)")

        section("pulizia")
        wipe(work)
        check("database lasciato pulito",
              seen_by_other("SELECT count(*) FROM jobs") , 0)

    finally:
        for c in (work, other):
            try:
                if not c.autocommit:
                    c.rollback()
                c.close()
            except Exception:  # noqa: BLE001, S110
                pass

    print(f"\n{len(PASSED)} superati, {len(FAILED)} falliti")
    if FAILED:
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("OK: i moduli committano davvero e rifiutano l'uso scorretto")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
