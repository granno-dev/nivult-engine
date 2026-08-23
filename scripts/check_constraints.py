#!/usr/bin/env python3
"""Verifica di comportamento: prova che i vincoli rifiutino davvero i dati incoerenti.

    python scripts/check_constraints.py

Ogni caso gira dentro un savepoint e viene annullato; l'intera sessione termina
con un ROLLBACK, quindi non lascia nulla. Resta comunque un guard-rail contro
l'esecuzione distratta su un database che non è di prova: serve un nome che
finisca per _test/_dev, oppure NIVULT_ALLOW_DESTRUCTIVE=1.

Un vincolo che non è mai stato visto scattare è un vincolo di cui non sai nulla.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from nivult.config import database_name, database_url, safe_dsn  # noqa: E402

VEC = "[" + ",".join(["0.01"] * 1024) + "]"

PASSED: list[str] = []
FAILED: list[str] = []


def expect_error(conn, sqlstate: str, label: str, sql: str, params=()) -> None:
    """Il caso DEVE fallire con questo SQLSTATE."""
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql, params)
    except psycopg.Error as exc:
        if exc.sqlstate == sqlstate:
            PASSED.append(label)
            print(f"  ok    {label}  [{sqlstate}]")
        else:
            FAILED.append(label)
            print(f"  FAIL  {label} — atteso {sqlstate}, ottenuto {exc.sqlstate}: "
                  f"{str(exc).strip().splitlines()[0]}")
        return
    FAILED.append(label)
    print(f"  FAIL  {label} — ACCETTATO, doveva essere rifiutato con {sqlstate}")


def expect_value(conn, label: str, sql: str, params, expected) -> None:
    """Il caso DEVE riuscire e produrre questo valore."""
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql, params)
                got = cur.fetchone()[0]
    except psycopg.Error as exc:
        FAILED.append(label)
        print(f"  FAIL  {label} — errore inatteso {exc.sqlstate}: "
              f"{str(exc).strip().splitlines()[0]}")
        return
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}  -> {got!r}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} — atteso {expected!r}, ottenuto {got!r}")


def section(title: str) -> None:
    print(f"\n{title}")


def fixtures(conn) -> dict[str, str]:
    """Dati minimi coerenti. Restano nella transazione esterna, annullata alla fine."""
    ids: dict[str, str] = {}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, plan, subscription_status, delivery_channel, "
            "frequency, timezone) VALUES "
            "('a@example.test','pro','active','email','daily','Europe/Rome') RETURNING id")
        ids["user_a"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO users (email, plan, subscription_status, delivery_channel, "
            "frequency, send_weekday, timezone) VALUES "
            "('b@example.test','basic','active','email','weekly',1,'UTC') "
            "RETURNING id")
        ids["user_b"] = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO clusters (family, country, daily_credit_cap) "
            "VALUES ('Human Resources','IT',100) RETURNING id")
        ids["cluster"] = cur.fetchone()[0]

        for key, sid, url in (
            ("job1", "f-1", "https://acme.example/careers/1"),
            ("job2", "f-2", "https://acme.example/careers/2"),
        ):
            cur.execute(
                "INSERT INTO jobs (source, source_job_id, url, canonical_url, "
                "domain_derived, title, title_normalized, organization, date_posted, "
                "countries, ai_key_skills, ai_requirements_summary, raw, link_kind) VALUES "
                "('fantastic', %s, %s, %s, 'acme.example', 'HR Manager', 'hr manager', "
                "'Acme', now(), ARRAY['IT'], ARRAY['recruiting','payroll'], "
                "'Cinque anni di esperienza', '{}'::jsonb, 'career_site') RETURNING id",
                (sid, url, url))
            ids[key] = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO matches (user_id, job_id, score, reason, threshold_used, model) "
            "VALUES (%s, %s, 91, 'Profilo allineato', 80, 'glm-5.2') RETURNING id",
            (ids["user_a"], ids["job1"]))
        ids["match_a"] = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO matches (user_id, job_id, score, reason, threshold_used, model) "
            "VALUES (%s, %s, 85, 'Anche lui', 80, 'glm-5.2') RETURNING id",
            (ids["user_b"], ids["job2"]))
        ids["match_b"] = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO digests (user_id, channel, scheduled_for, status) "
            "VALUES (%s, 'email', now(), 'pending') RETURNING id", (ids["user_a"],))
        ids["digest_a"] = cur.fetchone()[0]

        cur.execute(
            "INSERT INTO user_cvs (user_id, storage_key, embedding, embedding_model, "
            "encryption_algo, encrypted_dek, nonce, auth_tag, kek_version) "
            "VALUES (%s, 'cv/b.pdf', %s, 'bge-m3', 'aes-256-gcm', decode(repeat('ab',32),'hex'), decode(repeat('cd',12),'hex'), decode(repeat('ef',16),'hex'), 1)", (ids["user_b"], VEC))
        # Consumo attribuito a user_b: dopo la cancellazione la riga deve
        # sopravvivere senza il collegamento alla persona.
        cur.execute(
            "INSERT INTO api_usage (provider, operation, user_id, cost_micros) "
            "VALUES ('glm', 'score', %s, 1200)", (ids["user_b"],))
    return ids


def run_deletion(conn, label: str, user_id: str, max_batches: int = 50) -> str:
    """Cicla delete_user_batch come farebbe nivult.gdpr, e ritorna l'ultimo step."""
    step = None
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                for _ in range(max_batches):
                    cur.execute(
                        "SELECT step, rows_affected FROM delete_user_batch(%s, %s)",
                        (user_id, 1000))
                    step, _rows = cur.fetchone()
                    if step == "users":
                        break
                else:
                    raise RuntimeError("non converge entro max_batches")
    except (psycopg.Error, RuntimeError) as exc:
        FAILED.append(label)
        print(f"  FAIL  {label} — {exc}")
        return "errore"
    PASSED.append(label)
    print(f"  ok    {label}  -> ultimo step {step!r}")
    return step


def main() -> int:
    dsn = database_url()
    dbname = database_name(dsn)
    if not (dbname.endswith(("_test", "_dev")) or os.environ.get("NIVULT_ALLOW_DESTRUCTIVE") == "1"):
        print(f"rifiuto di girare su '{dbname}'.\n"
              f"Usa un database che finisca per _test/_dev, oppure esporta "
              f"NIVULT_ALLOW_DESTRUCTIVE=1 se sai cosa stai facendo.\n"
              f"(Lo script annulla tutto, ma non voglio che sia questo a proteggerti.)")
        return 2

    print(f"verifica vincoli — {safe_dsn(dsn)}")
    conn = psycopg.connect(dsn, autocommit=False)
    try:
        ids = fixtures(conn)
        u_a, u_b = ids["user_a"], ids["user_b"]
        cl, j1, j2 = ids["cluster"], ids["job1"], ids["job2"]

        section("users")
        expect_error(conn, "23514", "telegram senza chat_id rifiutato",
            "INSERT INTO users (email,plan,subscription_status,delivery_channel,frequency) "
            "VALUES ('t@example.test','pro','active','telegram','daily')")
        expect_error(conn, "23514", "weekly senza send_weekday rifiutato",
            "INSERT INTO users (email,plan,subscription_status,delivery_channel,frequency) "
            "VALUES ('w@example.test','pro','active','email','weekly')")
        expect_error(conn, "23514", "daily con send_weekday rifiutato",
            "INSERT INTO users (email,plan,subscription_status,delivery_channel,frequency,"
            "send_weekday) VALUES ('d@example.test','pro','active','email','daily',3)")
        expect_error(conn, "23514", "status deleted senza deleted_at rifiutato",
            "INSERT INTO users (email,plan,subscription_status,delivery_channel,frequency,"
            "status) VALUES ('x@example.test','pro','active','email','daily','deleted')")
        expect_error(conn, "23514", "fuso orario inesistente rifiutato",
            "INSERT INTO users (email,plan,subscription_status,delivery_channel,frequency,"
            "timezone) VALUES ('z@example.test','pro','active','email','daily','Europe/Atlantide')")
        expect_error(conn, "23505", "email duplicata rifiutata",
            "INSERT INTO users (email,plan,subscription_status,delivery_channel,frequency) "
            "VALUES ('a@example.test','pro','active','email','daily')")

        section("clusters e budget")
        expect_error(conn, "23514", "country minuscolo rifiutato",
            "INSERT INTO clusters (family,country,daily_credit_cap) VALUES ('X','it',10)")
        expect_error(conn, "23505", "cluster (famiglia,paese) duplicato rifiutato",
            "INSERT INTO clusters (family,country,daily_credit_cap) "
            "VALUES ('Human Resources','IT',50)")
        expect_error(conn, "23514", "tetto crediti a zero rifiutato",
            "INSERT INTO clusters (family,country,daily_credit_cap) VALUES ('Y','FR',0)")
        expect_error(conn, "23514", "circuit_open senza opened_at rifiutato",
            "INSERT INTO cluster_daily_budget (cluster_id,usage_date,circuit_open) "
            "VALUES (%s, current_date, true)", (cl,))

        section("circuit breaker")
        expect_value(conn, "consumo entro il tetto consentito",
            "SELECT cluster_try_consume(%s, 40)", (cl,), True)
        expect_value(conn, "consumo oltre il tetto rifiutato",
            "SELECT cluster_try_consume(%s, 5000)", (cl,), False)
        expect_value(conn, "tetto RICHIESTE indipendente dai crediti",
            "WITH c AS (INSERT INTO clusters (family,country,daily_credit_cap,daily_request_cap) "
            "  VALUES ('Gratis','FR',1000000,2) RETURNING id) "
            "SELECT cluster_try_consume(id,0)::text || cluster_try_consume(id,0)::text "
            "    || cluster_try_consume(id,0)::text FROM c", (), "truetruefalse")
        expect_value(conn, "breaker aperto dopo lo sforamento",
            "SELECT cluster_try_consume(%s, 5000) IS FALSE AND EXISTS ("
            "  SELECT 1 FROM cluster_daily_budget WHERE cluster_id=%s "
            "  AND usage_date=current_date AND circuit_open)", (cl, cl), True)

        section("iscrizioni")
        expect_error(conn, "23514", "min_seniority oltre max_seniority rifiutato",
            "INSERT INTO user_clusters (user_id,cluster_id,min_seniority,max_seniority) "
            "VALUES (%s,%s,'10+','0-2')", (u_a, cl))
        expect_error(conn, "23503", "cancellare un cluster con iscritti rifiutato",
            "WITH s AS (INSERT INTO user_clusters (user_id,cluster_id) VALUES (%s,%s) "
            "RETURNING cluster_id) DELETE FROM clusters WHERE id = (SELECT cluster_id FROM s)",
            (u_a, cl))

        section("offerte")
        expect_error(conn, "23505", "canonical_url duplicato rifiutato",
            "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,title_normalized,"
            "organization,date_posted,raw,link_kind) VALUES ('nav','n-1',"
            "'https://acme.example/careers/1','https://acme.example/careers/1',"
            "'Altro','altro','Acme',now(),'{}'::jsonb,'national_agency')")
        expect_error(conn, "23505", "(source, source_job_id) duplicato rifiutato",
            "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,title_normalized,"
            "organization,date_posted,raw,link_kind) VALUES ('fantastic','f-1',"
            "'https://acme.example/careers/9','https://acme.example/careers/9',"
            "'Altro','altro','Acme',now(),'{}'::jsonb,'career_site')")
        expect_error(conn, "23514", "status active con expired_at rifiutato",
            "UPDATE jobs SET expired_at = now() WHERE id = %s", (j1,))
        expect_error(conn, "23514", "offerta duplicato di se stessa rifiutata",
            "UPDATE jobs SET duplicate_of_job_id = id WHERE id = %s", (j1,))
        expect_error(conn, "23514", "catena di duplicati rifiutata",
            "WITH a AS (UPDATE jobs SET duplicate_of_job_id = %s WHERE id = %s RETURNING id) "
            "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,title_normalized,"
            "organization,date_posted,raw,link_kind,duplicate_of_job_id) "
            "SELECT 'nav','n-2','https://acme.example/careers/3',"
            "'https://acme.example/careers/3','Terzo','terzo','Acme',now(),"
            "'{}'::jsonb,'national_agency', id "
            "FROM a", (j1, j2))
        expect_value(conn, "trigger: tsv popolato e pesato",
            "SELECT tsv @@ to_tsquery('simple','manager') AND "
            "       tsv @@ to_tsquery('simple','payroll') FROM jobs WHERE id = %s", (j1,), True)
        expect_value(conn, "trigger: fingerprint popolato",
            "SELECT length(fingerprint) FROM jobs WHERE id = %s", (j1,), 64)
        expect_value(conn, "stesso annuncio da fonti diverse -> stesso fingerprint",
            "SELECT (SELECT fingerprint FROM jobs WHERE id=%s) = "
            "       (SELECT fingerprint FROM jobs WHERE id=%s)", (j1, j2), True)

        expect_error(conn, "23503", "tipo di link sconosciuto rifiutato",
            "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,title_normalized,"
            "organization,date_posted,raw,link_kind) VALUES ('nav','n-7',"
            "'https://acme.example/careers/7','https://acme.example/careers/7',"
            "'X','x','Acme',now(),'{}'::jsonb,'passaparola')")
        expect_error(conn, "23502", "offerta senza tipo di link rifiutata",
            "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,title_normalized,"
            "organization,date_posted,raw) VALUES ('nav','n-8',"
            "'https://acme.example/careers/8','https://acme.example/careers/8',"
            "'X','x','Acme',now(),'{}'::jsonb)")
        expect_value(conn, "career_site precede national_agency nell'ordinamento",
            "SELECT (SELECT rank FROM link_kinds WHERE kind='career_site') < "
            "       (SELECT rank FROM link_kinds WHERE kind='national_agency')", (), True)
        expect_value(conn, "solo career_site è candidatura diretta",
            "SELECT string_agg(kind, ',' ORDER BY rank) FROM link_kinds WHERE is_direct",
            (), "career_site")

        section("match")
        expect_error(conn, "23514", "punteggio fuori scala rifiutato",
            "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model) "
            "VALUES (%s,%s,150,'x',80,'glm-5.2')", (u_a, j2))
        expect_error(conn, "23505", "ANTI-RIPETIZIONE: stessa offerta allo stesso utente",
            "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model) "
            "VALUES (%s,%s,70,'seconda valutazione',80,'glm-5.2')", (u_a, j1))
        expect_value(conn, "colonna generata passed coerente col punteggio",
            "SELECT passed FROM matches WHERE id = %s", (ids["match_a"],), True)
        expect_error(conn, "23503", "cancellare un'offerta già valutata rifiutato",
            "DELETE FROM jobs WHERE id = %s", (j1,))

        section("digest")
        expect_error(conn, "23505", "doppio digest sulla stessa finestra rifiutato",
            "INSERT INTO digests (user_id,channel,scheduled_for,status) "
            "SELECT user_id, 'email', scheduled_for, 'pending' FROM digests WHERE id = %s",
            (ids["digest_a"],))
        expect_error(conn, "23514", "status sent senza sent_at rifiutato",
            "UPDATE digests SET status = 'sent' WHERE id = %s", (ids["digest_a"],))
        expect_error(conn, "23514", "inviate più di quelle valutate rifiutato",
            "UPDATE digests SET jobs_sent_count = 5, jobs_evaluated_count = 2 WHERE id = %s",
            (ids["digest_a"],))
        expect_error(conn, "23503", "voce di digest col match di un ALTRO utente rifiutata",
            "INSERT INTO digest_items (digest_id,job_id,user_id,match_id,rank,"
            "score_snapshot,reason_snapshot) VALUES (%s,%s,%s,%s,1,85,'x')",
            (ids["digest_a"], j2, u_a, ids["match_b"]))
        expect_error(conn, "23505", "rank duplicato nello stesso digest rifiutato",
            "WITH a AS (INSERT INTO digest_items (digest_id,job_id,user_id,match_id,rank,"
            "score_snapshot,reason_snapshot) VALUES (%s,%s,%s,%s,1,91,'ok') RETURNING 1) "
            "INSERT INTO digest_items (digest_id,job_id,user_id,match_id,rank,"
            "score_snapshot,reason_snapshot) VALUES (%s,%s,%s,%s,1,85,'ok')",
            (ids["digest_a"], j1, u_a, ids["match_a"],
             ids["digest_a"], j2, u_a, ids["match_a"]))

        section("autenticazione senza password")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO login_tokens (user_id, token_hash, expires_at) "
                        "VALUES (%s, repeat('a',64), now() + interval '15 min')", (u_a,))
            cur.execute("INSERT INTO oauth_identities (provider, subject, user_id) "
                        "VALUES ('google','sub-123',%s)", (u_a,))
            cur.execute("INSERT INTO sessions (user_id, token_hash, expires_at, origin) "
                        "VALUES (%s, repeat('b',64), now() + interval '30 days','magic_link')",
                        (u_a,))

        expect_error(conn, "23505", "token di accesso duplicato rifiutato",
            "INSERT INTO login_tokens (user_id, token_hash, expires_at) "
            "VALUES (%s, repeat('a',64), now() + interval '15 min')", (u_a,))
        expect_error(conn, "23514", "token_hash che non è sha256 esadecimale rifiutato",
            "INSERT INTO login_tokens (user_id, token_hash, expires_at) "
            "VALUES (%s, 'non-e-un-hash', now() + interval '15 min')", (u_a,))
        expect_error(conn, "23514", "token già scaduto alla creazione rifiutato",
            "INSERT INTO login_tokens (user_id, token_hash, expires_at) "
            "VALUES (%s, repeat('c',64), now() - interval '1 min')", (u_a,))
        expect_error(conn, "23505", "stessa identità OAuth su due account rifiutata",
            "INSERT INTO oauth_identities (provider, subject, user_id) "
            "VALUES ('google','sub-123',%s)", (u_b,))
        expect_error(conn, "23505", "due identità Google per lo stesso utente rifiutate",
            "INSERT INTO oauth_identities (provider, subject, user_id) "
            "VALUES ('google','sub-999',%s)", (u_a,))
        expect_error(conn, "23514", "origine di sessione sconosciuta rifiutata",
            "INSERT INTO sessions (user_id, token_hash, expires_at, origin) "
            "VALUES (%s, repeat('d',64), now() + interval '1 day','facebook')", (u_a,))
        expect_value(conn, "nessuna colonna password in tutto lo schema",
            "SELECT count(*) FROM information_schema.columns WHERE table_schema='public' "
            "AND column_name ILIKE '%%password%%'", (), 0)

        section("cifratura dei CV")
        expect_error(conn, "23514", "DEK troppo corta rifiutata",
            "INSERT INTO user_cvs (user_id, storage_key, encryption_algo, encrypted_dek, "
            "nonce, auth_tag, kek_version) VALUES (%s,'cv/x.pdf','aes-256-gcm',"
            "decode('aabb','hex'), decode(repeat('cd',12),'hex'), "
            "decode(repeat('ef',16),'hex'), 1)", (u_a,))
        expect_error(conn, "23514", "nonce di lunghezza sbagliata rifiutato",
            "INSERT INTO user_cvs (user_id, storage_key, encryption_algo, encrypted_dek, "
            "nonce, auth_tag, kek_version) VALUES (%s,'cv/y.pdf','aes-256-gcm',"
            "decode(repeat('ab',32),'hex'), decode(repeat('cd',8),'hex'), "
            "decode(repeat('ef',16),'hex'), 1)", (u_a,))
        expect_error(conn, "23514", "algoritmo non previsto rifiutato",
            "INSERT INTO user_cvs (user_id, storage_key, encryption_algo, encrypted_dek, "
            "nonce, auth_tag, kek_version) VALUES (%s,'cv/z.pdf','rot13',"
            "decode(repeat('ab',32),'hex'), decode(repeat('cd',12),'hex'), "
            "decode(repeat('ef',16),'hex'), 1)", (u_a,))
        expect_value(conn, "purge_expired_auth non tocca token ancora validi",
            "SELECT sum(rows_affected) FROM purge_expired_auth(7)", (), 0)

        section("retention")
        with conn.cursor() as cur:
            # Offerte dedicate, per non interferire con le altre sezioni.
            def mkjob(sid, status, expired_days, posted_days, salary=False):
                cur.execute(
                    "INSERT INTO jobs (source,source_job_id,url,canonical_url,domain_derived,"
                    "title,title_normalized,organization,date_posted,countries,ai_key_skills,"
                    "salary,status,expired_at,raw,link_kind) VALUES ('nav',%s,%s,%s,'ret.example',"
                    "'Retention','retention','RetCo', now() - make_interval(days => %s::int),"
                    "ARRAY['IT'],ARRAY['sql'],%s::jsonb,%s,"
                    "CASE WHEN %s::int IS NULL THEN NULL "
                    "ELSE now() - make_interval(days => %s::int) END,"
                    "'{\"big\": \"payload\"}'::jsonb,'national_agency') RETURNING id",
                    (sid, f"https://ret.example/{sid}", f"https://ret.example/{sid}",
                     posted_days, '{"min": 1}' if salary else None, status,
                     expired_days, expired_days))
                jid = cur.fetchone()[0]
                cur.execute("INSERT INTO job_clusters (job_id,cluster_id) VALUES (%s,%s)", (jid, cl))
                cur.execute("INSERT INTO job_embeddings (job_id,embedding,model) "
                            "VALUES (%s,%s,'bge-m3')", (jid, VEC))
                return jid

            j_free   = mkjob("ret-free",   "expired", 90, 120, salary=True)
            j_ref    = mkjob("ret-ref",    "expired", 90, 120)
            j_recent = mkjob("ret-recent", "expired", 10, 40)
            j_active = mkjob("ret-active", "active",  None, 1200)
            cur.execute(
                "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model) "
                "VALUES (%s,%s,88,'gia valutata',80,'glm-5.2')", (u_a, j_ref))

        expect_value(conn, "un lotto elimina 1 offerta e ne svuota 1",
            "SELECT jobs_deleted || '/' || jobs_tombstoned FROM purge_dead_jobs(60, 100)",
            (), "1/1")
        expect_value(conn, "offerta morta non referenziata: cancellata",
            "SELECT count(*) FROM jobs WHERE id = %s", (j_free,), 0)
        expect_value(conn, "offerta morta referenziata: resta come lapide, senza raw",
            "SELECT raw IS NULL AND purged_at IS NOT NULL AND tsv IS NULL "
            "FROM jobs WHERE id = %s", (j_ref,), True)
        expect_value(conn, "lapide: embedding rimosso",
            "SELECT count(*) FROM job_embeddings WHERE job_id = %s", (j_ref,), 0)
        expect_value(conn, "lapide: il match sopravvive, anti-ripetizione intatta",
            "SELECT count(*) FROM matches WHERE job_id = %s", (j_ref,), 1)
        expect_value(conn, "morta da soli 10 giorni: non toccata",
            "SELECT raw IS NOT NULL AND purged_at IS NULL FROM jobs WHERE id = %s",
            (j_recent,), True)
        expect_value(conn, "ATTIVA da 1200 giorni: non scade mai",
            "SELECT raw IS NOT NULL AND purged_at IS NULL FROM jobs WHERE id = %s",
            (j_active,), True)
        expect_value(conn, "contatori aggregati scritti prima della cancellazione",
            "SELECT jobs_purged || '/' || jobs_tombstoned || '/' || expired_count "
            "|| '/' || with_salary_count FROM cluster_month_stats "
            "WHERE cluster_id = %s", (cl,), "1/1/2/1")
        expect_value(conn, "vita media calcolata sulle offerte eliminate",
            "SELECT avg_lifetime_days = 30 FROM cluster_month_stats_v WHERE cluster_id = %s",
            (cl,), True)
        expect_value(conn, "aggregati: nessuna colonna che riferisca un utente",
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'cluster_month_stats' AND column_name LIKE '%%user%%'", (), 0)
        expect_value(conn, "secondo giro: niente da fare (idempotente)",
            "SELECT jobs_deleted || '/' || jobs_tombstoned FROM purge_dead_jobs(60, 100)",
            (), "0/0")
        expect_error(conn, "23514", "raw NULL senza purged_at rifiutato",
            "UPDATE jobs SET raw = NULL WHERE id = %s", (j_recent,))
        expect_error(conn, "23514", "offerta attiva marcata come svuotata rifiutata",
            "UPDATE jobs SET purged_at = now() WHERE id = %s", (j_active,))
        expect_error(conn, "23514", "mese che non è il primo del mese rifiutato",
            "INSERT INTO cluster_month_stats (cluster_id, month) VALUES (%s, DATE '2026-03-15')",
            (cl,))

        section("cancellazione GDPR")
        expect_value(conn, "prima: user_b ha un CV e una riga di consumo",
            "SELECT (SELECT count(*) FROM user_cvs WHERE user_id=%s) = 1 AND "
            "       (SELECT count(*) FROM api_usage WHERE user_id=%s) = 1", (u_b, u_b), True)
        run_deletion(conn, "delete_user_batch svuota e arriva a 'users'", u_b)
        expect_value(conn, "dopo: l'utente non esiste più",
            "SELECT count(*) FROM users WHERE id = %s", (u_b,), 0)
        expect_value(conn, "dopo: nessun CV residuo",
            "SELECT count(*) FROM user_cvs WHERE user_id = %s", (u_b,), 0)
        expect_value(conn, "dopo: nessun token, sessione o identità OAuth residua",
            "SELECT (SELECT count(*) FROM login_tokens WHERE user_id=%s) "
            "     + (SELECT count(*) FROM sessions WHERE user_id=%s) "
            "     + (SELECT count(*) FROM oauth_identities WHERE user_id=%s)",
            (u_b, u_b, u_b), 0)
        expect_value(conn, "dopo: il costo sopravvive senza collegamento alla persona",
            "SELECT count(*) FROM api_usage WHERE user_id IS NULL AND cost_micros = 1200",
            (), 1)

    finally:
        conn.rollback()
        conn.close()

    print(f"\n{len(PASSED)} superati, {len(FAILED)} falliti")
    if FAILED:
        for f in FAILED:
            print(f"  - {f}")
        return 1
    print("OK: i vincoli rifiutano ciò che devono rifiutare")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
