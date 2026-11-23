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

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import psycopg  # noqa: E402

from nivult.ats import tassonomie as tassonomie_mod  # noqa: E402
from nivult import auth  # noqa: E402
from nivult import crypto  # noqa: E402
from nivult.config import database_name, database_url, safe_dsn  # noqa: E402
from nivult.delivery import push as push_mod  # noqa: E402
from nivult.gdpr import execute_deletion, request_deletion  # noqa: E402
from nivult.matching import colloquio as colloquio_mod  # noqa: E402
from nivult.matching import funnel as funnel_mod  # noqa: E402
from nivult.matching import llm as llm_mod  # noqa: E402
from nivult.matching import lettera as lettera_mod  # noqa: E402
from nivult.matching import m2v as m2v_mod  # noqa: E402
from nivult.matching import micro as micro_mod  # noqa: E402
from nivult.matching import worker  # noqa: E402
from nivult.matching.funnel import candidati  # noqa: E402
from nivult.retention import purge  # noqa: E402

from datetime import datetime, timedelta, timezone  # noqa: E402
import hashlib  # noqa: E402
import os as _os  # noqa: E402
import secrets as _secrets  # noqa: E402

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

        cur.execute("INSERT INTO users (email,plan,subscription_status,delivery_channels,"
                    "frequency,timezone) VALUES ('mod@example.test','pro','active','{email}',"
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
            cur.execute("INSERT INTO job_embeddings (job_id,embedding,model,content_hash) "
                        "VALUES (%s,%s,'bge-m3',md5(%s))", (jid, VEC, sid))
            return jid

        ids["job_free"] = mkjob("free", "expired", 90, 150)     # morta, mai valutata
        ids["job_sent"] = mkjob("sent", "expired", 90, 150)     # morta, ma inviata
        ids["job_live"] = mkjob("live", "active", None, 900)    # viva e vecchissima

        cur.execute("INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model,"
                    "saved_at,application_status,applied_at,action_version,user_action_at) "
                    "VALUES (%s,%s,93,'Profilo allineato',80,'glm-5.2',"
                    "now() - interval '10 days','applied',now() - interval '9 days',"
                    "1,now()) RETURNING id",
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
        cur.execute(
            "INSERT INTO match_interviews (match_id,user_id,starts_at,reminder_minutes) "
            "VALUES (%s,%s,now() + interval '2 days',60)", (ids["match"], ids["user"]))
        cur.execute(
            "INSERT INTO match_documents (match_id,user_id,original_filename,mime_type,"
            "size_bytes,sha256,storage_key,encryption_algo,encrypted_dek,nonce,auth_tag,"
            "kek_version) VALUES (%s,%s,'lettera.pdf','application/pdf',100,repeat('a',64),"
            "'documenti/mod/lettera.pdf','aes-256-gcm',decode(repeat('ab',32),'hex'),"
            "decode(repeat('cd',12),'hex'),decode(repeat('ef',16),'hex'),1)",
            (ids["match"], ids["user"]))
        cur.execute(
            "INSERT INTO personal_notification_dedup (user_id,kind,event_key) "
            "VALUES (%s,'weekly','2026-10-26')", (ids["user"],))
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
                "INSERT INTO users (email, plan, subscription_status, delivery_channels, "
                "  frequency, timezone, next_digest_at) VALUES "
                "(%s,'pro','active','{email}','daily','Europe/Rome', now() - make_interval(mins => %s::int)) "
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
                "  max_seniority, employment_types, wants, target_role, "
                "  desired_technologies) VALUES "
                "(%s,%s, ARRAY['Italian'], '2-5', '10+', ARRAY['FULL_TIME'], "
                " 'Voglio crescere nel recruiting', 'HR Business Partner', "
                " ARRAY['Python'])",
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
                "  ai_key_skills, ai_requirements_summary, ai_core_responsibilities, "
                "  ai_working_hours, raw, link_kind, org_headcount) VALUES "
                "('fantastic', %s, %s, %s, 'acme.example', %s, %s, 'Acme SpA', "
                "  now() - make_interval(days => %s::int), ARRAY['IT'], %s, %s, %s, "
                "  ARRAY['recruiting'], 'requisiti finti', 'responsabilità finte', "
                "  'orario finto', jsonb_build_object('ai_benefits', 'benefit finti'), "
                "  %s, 500) RETURNING id",
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
        offerta("junior senza tag", esp=None)            # prova testuale nel titolo

        # B ha il budget del mese già consumato: il breaker deve dire di no.
        cur.execute(
            "INSERT INTO user_evaluation_budget (user_id, period_month, evaluations_used) "
            "VALUES (%s, date_trunc('month', current_date)::date, 4000)", (ids["b"],))
    conn.commit()
    return ids


class ValutatoreFinto:
    """Il contratto del valutatore, con punteggi decisi a tavolino."""

    def __init__(self, punteggi: dict[str, int], provider="groq"):
        self.punteggi = punteggi
        self.model = mock.Mock(source=provider, model=f"{provider}-finto")
        self.totale = {"input": 0, "cached": 0, "output": 0, "chiamate": 0}
        self.motivazioni_viste = []

    def valuta(self, profilo_testo, offerta, lingua="English"):
        sid = offerta["source_job_id"]
        self.totale["chiamate"] += 1
        self.totale["input"] += 100
        self.totale["output"] += 10
        return self.punteggi.get(sid, 10), f"motivo breve {sid}", {
            "input": 100, "output": 10,
            "model": self.model.model, "provider": self.model.source}

    def motiva(self, profilo_testo, offerta, lingua="English",
               locale: str = "en", evita=None):
        sid = offerta["source_job_id"]
        self.totale["chiamate"] += 1
        self.motivazioni_viste.append(dict(offerta))
        return (f"motivazione piena {sid}", {},
                {"input": 80, "output": 20, "model": "scrittore-finto",
                 "provider": "scrittore-locale"})


class ModelloFinto:
    def __init__(self, provider, model, risposte, *, usage=None):
        self.source = provider
        self.model = model
        self.risposte = list(risposte)
        self.chiamate = []
        self.last_usage = {}
        self.usage = usage or {"input": 100, "cached": 20,
                               "output": 10, "complete": True}
        self.chiuso = False

    def chat(self, messages, **kw):
        self.chiamate.append(messages)
        self.last_usage = dict(self.usage)
        risposta = self.risposte.pop(0)
        if isinstance(risposta, Exception):
            raise risposta
        return risposta if isinstance(risposta, str) else json.dumps(risposta)

    def close(self):
        self.chiuso = True


def valutatore_da(modelli, scrittore=None):
    valutatore = object.__new__(worker.ValutatoreGLM)
    valutatore.model = modelli[0]
    valutatore._ripieghi = list(modelli[1:])
    valutatore.scrittore = scrittore
    valutatore.totale = {"input": 0, "cached": 0, "output": 0, "chiamate": 0}
    valutatore.totali = {}
    return valutatore


def check_budget_provider(work, seen_by_other) -> None:
    section("budget delle valutazioni provider-aware")
    wipe(work)
    with work.cursor() as cur:
        cur.execute("INSERT INTO clusters (family, country) "
                    "VALUES ('Human Resources', 'IT') RETURNING id")
        cluster_id = cur.fetchone()[0]
        for sid in ("budget-1", "budget-2"):
            cur.execute(
                "INSERT INTO jobs (source, source_job_id, url, canonical_url, "
                "  domain_derived, title, title_normalized, organization, date_posted, "
                "  countries, ai_job_language, ai_experience_level, ai_employment_type, "
                "  ai_key_skills, ai_requirements_summary, ai_core_responsibilities, "
                "  ai_working_hours, raw, link_kind) VALUES "
                "('fantastic', %s, %s, %s, 'acme.example', 'HR Manager', "
                " 'hr manager', 'Acme', now(), ARRAY['IT'], 'Italian', '5-10', "
                " 'FULL_TIME', ARRAY['recruiting'], 'requisiti veri', "
                " 'responsabilità vere', 'orario vero', "
                " jsonb_build_object('ai_benefits', 'benefit veri'), 'career_site') "
                "RETURNING id",
                (sid, f"https://acme.example/{sid}", f"https://acme.example/{sid}"))
            job_id = cur.fetchone()[0]
            cur.execute("INSERT INTO job_clusters (job_id, cluster_id) VALUES (%s, %s)",
                        (job_id, cluster_id))

        def utente(email, usate=None):
            cur.execute(
                "INSERT INTO users (email, plan, subscription_status, "
                "  delivery_channels, frequency, timezone, next_digest_at) "
                "VALUES (%s, 'pro', 'active', '{app}', 'daily', 'Europe/Rome', "
                " now() - interval '10 min') RETURNING id", (email,))
            uid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_cvs (user_id, storage_key, families, seniority, "
                "  skills, languages, years_experience, encryption_algo, encrypted_dek, "
                "  nonce, auth_tag, kek_version) VALUES "
                "(%s, 'cv/budget.pdf', ARRAY['Human Resources'], '5-10', "
                " ARRAY['recruiting'], '[\"Italian\"]'::jsonb, 8, 'aes-256-gcm', "
                " decode(repeat('ab',32),'hex'), decode(repeat('cd',12),'hex'), "
                " decode(repeat('ef',16),'hex'), 1)", (uid,))
            cur.execute("INSERT INTO user_clusters (user_id, cluster_id, wants) "
                        "VALUES (%s, %s, 'Voglio il recruiting')", (uid, cluster_id))
            if usate is not None:
                cur.execute(
                    "INSERT INTO user_evaluation_budget "
                    "  (user_id, period_month, evaluations_used) "
                    "VALUES (%s, date_trunc('month', current_date)::date, %s)",
                    (uid, usate))
            return uid

        uid_locale = utente("budget-local-zero@example.test", 4000)
        uid_esterno = utente("budget-external-ok@example.test")
        uid_esaurito = utente("budget-external-zero@example.test", 4000)
        uid_fallback = utente("budget-fallback@example.test", 4000)
        uid_invalido = utente("budget-invalid@example.test")
        uid_recovery = utente("budget-recovery@example.test")
        utente("ann-fallback@example.test")
    work.commit()

    from psycopg.rows import dict_row
    with work.cursor(row_factory=dict_row) as cur:
        utenti = {u.email: u for u in worker.utenti_dovuti(
            cur, datetime.now(timezone.utc))}

    def budget(uid):
        return seen_by_other(
            "SELECT evaluations_used FROM user_evaluation_budget WHERE user_id = %s",
            (uid,))

    def match(uid):
        return seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s", (uid,))

    with (mock.patch.object(worker.MaestroLocale, "disponibile", return_value=True),
          mock.patch.object(worker.prescore_v3, "disponibile", return_value=False),
          mock.patch.object(worker.prescore_mod, "disponibile", return_value=False),
          mock.patch.object(worker.prescore_mod, "family_guard_disponibile",
                            return_value=False)):
        locale = ValutatoreFinto({}, provider="maestro-locale")
        esito_locale = worker.digest_utente(
            work, utenti["budget-local-zero@example.test"], dry_run=False,
            evaluatore=locale)
        esterno = ValutatoreFinto({})
        esito_esterno = worker.digest_utente(
            work, utenti["budget-external-ok@example.test"], dry_run=False,
            evaluatore=esterno)
        esaurito = ValutatoreFinto({})
        esito_esaurito = worker.digest_utente(
            work, utenti["budget-external-zero@example.test"], dry_run=False,
            evaluatore=esaurito)

        locale_rotto = ModelloFinto(
            "maestro-locale", "locale-invalido", [{"reason": "manca"}])
        esterno_ripiego = ModelloFinto(
            "groq", "groq-emergenza", [{"score": 10, "reason": "ripiego"}])
        esito_fallback = worker.digest_utente(
            work, utenti["budget-fallback@example.test"], dry_run=False,
            evaluatore=valutatore_da([locale_rotto, esterno_ripiego]))

        try:
            worker.digest_utente(
                work, utenti["budget-invalid@example.test"], dry_run=False,
                evaluatore=valutatore_da([
                    ModelloFinto("groq", "groq-invalido", [{"score": None}])]))
        except RuntimeError:
            ok("risposta invalida esterna arriva all'errore senza addebito")
        else:
            bad("risposta invalida esterna arriva all'errore senza addebito",
                "risposta accettata")

        esterno_rotto = ModelloFinto(
            "groq", "groq-recovery", [{"reason": "manca"}])
        locale_recovery = ModelloFinto("maestro-locale", "locale-recovery", [
            {"score": 10, "reason": "primo"}, {"score": 10, "reason": "secondo"}])
        esito_recovery = worker.digest_utente(
            work, utenti["budget-recovery@example.test"], dry_run=False,
            evaluatore=valutatore_da([esterno_rotto, locale_recovery]))

    chiamate_ann = []

    def ann_con_guasto(*args, **kwargs):
        usa_ann = kwargs.get("vettore") is not None
        chiamate_ann.append(usa_ann)
        if usa_ann:
            raise RuntimeError("indice simulato non disponibile")
        return []

    with (mock.patch.dict(os.environ, {"PRESCORE_V3_ANN": "1"}),
          mock.patch.object(worker.prescore_v3, "disponibile", return_value=True),
          mock.patch.object(worker.prescore_v3, "query_embedding",
                            return_value=[0.01] * 1024),
          mock.patch.object(worker.funnel, "copertura_embedding", return_value=1.0),
          mock.patch.object(worker.funnel, "candidati", side_effect=ann_con_guasto)):
        esito_ann = worker.digest_utente(
            work, utenti["ann-fallback@example.test"], dry_run=True,
            evaluatore=ValutatoreFinto({}))
    check("guasto ANN ricade sul funnel completo senza rompere il digest",
          (chiamate_ann, esito_ann["stato"]), ([True, False], "skipped_empty"))

    with (mock.patch.dict(os.environ, {"PRESCORE_V3_ANN": "1"}),
          mock.patch.object(worker.prescore_v3, "disponibile", return_value=True),
          mock.patch.object(worker.prescore_v3, "query_embedding") as embedding,
          mock.patch.object(worker.funnel, "copertura_embedding", return_value=0.97),
          mock.patch.object(worker.funnel, "candidati", return_value=[]) as candidati):
        esito_copertura = worker.digest_utente(
            work, utenti["ann-fallback@example.test"], dry_run=True,
            evaluatore=ValutatoreFinto({}))
    check("sotto 98% non richiede il vettore e usa il funnel completo",
          (embedding.call_count, candidati.call_args.kwargs["vettore"],
           esito_copertura["stato"]), (0, None, "skipped_empty"))

    check("locale a budget zero non è bloccato e non consuma quota",
          (esito_locale["stato"], esito_locale["valutate"], budget(uid_locale),
           match(uid_locale), locale.totale["chiamate"]),
          ("skipped_empty", 2, 4000, 2, 2))
    check("match locale conserva provider, modello e token",
          seen_by_other(
              "SELECT bool_and(provider = 'maestro-locale' "
              "  AND model = 'maestro-locale-finto' "
              "  AND input_tokens = 100 AND output_tokens = 10) "
              "FROM matches WHERE user_id = %s", (uid_locale,)), True)
    check("esterno con budget disponibile paga una quota per offerta valida",
          (esito_esterno["stato"], esito_esterno["valutate"], budget(uid_esterno),
           match(uid_esterno), esterno.totale["chiamate"]),
          ("skipped_empty", 2, 2, 2, 2))
    check("esterno esaurito è bloccato prima della chiamata",
          (esito_esaurito["stato"], esito_esaurito["valutate"], budget(uid_esaurito),
           match(uid_esaurito), esaurito.totale["chiamate"]),
          ("failed_budget", 0, 4000, 0, 0))
    check("fallback locale→esterno è gratuito solo nell'emergenza",
          (esito_fallback["valutate"], budget(uid_fallback), match(uid_fallback),
           len(esterno_ripiego.chiamate)), (1, 4000, 1, 1))
    check("il match del fallback registra il provider esterno davvero usato",
          seen_by_other("SELECT provider FROM matches WHERE user_id = %s",
                        (uid_fallback,)), "groq")
    check("risposta invalida non lascia match né consuma budget",
          (match(uid_invalido),
           seen_by_other("SELECT count(*) FROM user_evaluation_budget "
                         "WHERE user_id = %s", (uid_invalido,))), (0, 0))
    check("recovery esterno→locale: un solo addebito, poi locale gratuito",
          (esito_recovery["valutate"], budget(uid_recovery), match(uid_recovery),
           len(locale_recovery.chiamate)), (2, 1, 2, 2))


def check_match_micro() -> None:
    section("match-micro: modello e requisiti obbligatori")
    profilo = ("[1] Ruolo cercato: HR Business Partner\n[2] Seniority: 5-10\n"
               "[3] Competenza: HRIS\n[4] Lingua: Italian\n[5] Lingua: English\n"
               "[6] Sedi accettate: Italia, Europa, remoto")
    modello = {"versione": micro_mod.VERSIONE, "n_features": micro_mod.N_FEATURES,
               "weights": [0.0] * (micro_mod.N_FEATURES + 20),
               "media": 80.0, "bias": 0.0, "dense_mean": [0.0] * 20,
               "dense_std": [1.0] * 20}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "match-micro.json"
        path.write_text(json.dumps(modello))
        scorer = micro_mod.MicroScorer(path)
        base = {"title": "HR Business Partner", "ai_experience_level": "senior",
                "ai_requirements_summary": "HRIS e employee relations"}
        check("artefatto match-micro caricato e punteggio base calcolato",
              scorer.score(profilo, base), 80.0)
        check("lingua obbligatoria assente limita il punteggio a 45",
              scorer.score(profilo, dict(base, ai_requirements_summary="German required")), 45.0)
        check("lingua richiesta e presente non attiva alcuna penalità",
              scorer.score(profilo, dict(base, ai_requirements_summary="English required")), 80.0)
        check("internship per profilo senior limita il punteggio a 45",
              scorer.score(profilo, dict(base, title="HR Internship",
                                           ai_experience_level="intern")), 45.0)


def check_m2v() -> None:
    section("Model2Vec: caricamento e contratto dello scorer")
    class FintoStatic:
        @classmethod
        def from_pretrained(cls, _path):
            return cls()

        def encode(self, testi):
            import numpy as np
            return np.array([[1.0] + [0.0] * 63 for _ in testi])

    config = {"versione": m2v_mod.VERSIONE, "weights": [0.0] * 149,
              "media": 80.0, "bias": 0.0, "feature_mean": [0.0] * 149,
              "feature_std": [1.0] * 149}
    profilo = ("[1] Ruolo cercato: HR Business Partner\n[2] Seniority: 5-10\n"
               "[3] Competenza: HRIS\n[4] Lingua: Italian\n[5] Lingua: English\n"
               "[6] Sedi accettate: Italia, Europa, remoto")
    job = {"title": "HR Business Partner", "ai_experience_level": "senior",
           "ai_requirements_summary": "English required"}
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "scoring.json").write_text(json.dumps(config))
        with mock.patch.dict(sys.modules, {"model2vec": mock.Mock(StaticModel=FintoStatic)}):
            scorer = m2v_mod.M2VScorer(tmp)
            check("artefatto M2V caricato e scoring eseguito", scorer.score(profilo, job), 80.0)
            check("batch M2V vuoto", scorer.score_batch(profilo, []), [])
            check("batch M2V mantiene ordine e valori",
                  scorer.score_batch(profilo, [job, dict(job, ai_requirements_summary="German required")]),
                  [80.0, 45.0])


def check_modelli_e_analisi() -> None:
    section("modelli, score e anti-invenzione (solo mock)")
    check_match_micro()
    check_m2v()
    variabili = {nome: "" for nome in (
        "MAESTRO_URL", "SCRITTORE_URL", "GLM_API_KEY", "GROQ_API_KEY",
        "NVIDIA_API_KEY", "MISTRAL_API_KEY")}
    with mock.patch.dict(os.environ, variabili):
        try:
            worker.ValutatoreGLM()
        except RuntimeError as exc:
            check("nessun modello configurato è un errore esplicito",
                  "nessun modello" in str(exc), True)
        else:
            bad("nessun modello configurato è un errore esplicito", "nessun errore")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "finto"}):
            try:
                valutatore = worker.ValutatoreGLM()
            except SystemExit as exc:
                bad("senza GLM_API_KEY si arriva al fallback", str(exc))
            else:
                check("senza GLM_API_KEY il primo endpoint è Groq",
                      isinstance(valutatore.model, worker.Groq), True)
                valutatore.__exit__()

    profilo = "[1] Competenza: recruiting"
    offerta = {"id": "job-1", "title": "HR Manager",
               "_wants": "Voglio crescere nel recruiting"}
    for indice, payload in enumerate(({}, {"score": None}, {"score": True},
                                      {"score": "non-numero"}, {"score": "NaN"})):
        modello = ModelloFinto("groq", "rotto", [payload])
        try:
            llm_mod.valuta_offerta(modello, profilo, offerta)
        except llm_mod.RispostaModelloNonValida:
            ok(f"score malformato {indice + 1} rifiutato", "fallback possibile")
        else:
            bad(f"score malformato {indice + 1} rifiutato", "accettato")

    modello = ModelloFinto("groq", "zero-valido", [{"score": 0, "reason": "no"}])
    score, _, _ = llm_mod.valuta_offerta(modello, profilo, offerta)
    check("score zero esplicito resta valido", score, 0)
    check("il formatter di valutazione include `_wants`",
          "Voglio crescere" in modello.chiamate[0][-1]["content"], True)
    profilo_geo = llm_mod.profilo_come_testo({
        "ruolo": "Human Resources", "residenza": "Rome · IT",
        "note": "Disponibile a trasferirsi"})
    check("residenza e trasferimento sono fatti distinti del profilo",
          ("Città e paese dichiarati nel CV: Rome · IT" in profilo_geo,
           "Disponibile a trasferirsi" in profilo_geo, "Sedi accettate" in profilo_geo),
          (True, True, False))
    prompt_geo = llm_mod.offerta_come_testo({
        "id": "rtx", "title": "HR Employee Partner", "cities": ["Rome"],
        "countries": ["US"]})
    check("il paese dell'offerta accompagna sempre la città omonima",
          ("sede: Rome" in prompt_geo, "paese: US" in prompt_geo), (True, True))
    prompt_fonte = llm_mod.offerta_come_testo({
        "id": "fonte", "title": "HR Employee Partner",
        "ai_core_responsibilities": "Manage collective bargaining and workplace investigations"})
    check("senza riassunto il punteggio legge la descrizione reale",
          ("descrizione: Manage collective bargaining" in prompt_fonte,
           "workplace investigations" in prompt_fonte), (True, True))
    prompt_lungo = llm_mod.offerta_come_testo({
        "id": "senior-vicenza", "title": "Senior Consultant - Vicenza",
        "ai_requirements_summary": "A" * 500 + " sviluppo commerciale e Permanent Placement"})
    check("il prompt non taglia le mansioni oltre i 400 caratteri",
          "Permanent Placement" in prompt_lungo, True)
    check("il titolo junior scarta anche senza il metadato di seniority",
          funnel_mod.titolo_sotto_seniority("Addetto HR junior", "5-10"), True)
    check("lo stage nel titolo usa la prova lessicale esistente",
          funnel_mod.titolo_sotto_seniority("Stage HR", "2-5", "Italian"), True)
    check("entry level nel titolo è un'evidenza di seniority",
          funnel_mod.titolo_sotto_seniority("HR Generalist - Entry Level", "5-10", "en"),
          True)
    check("nessuna seniority minima non attiva il filtro testuale",
          funnel_mod.titolo_sotto_seniority("Addetto HR junior", None), False)
    check("una ricerca junior non scarta il titolo junior",
          funnel_mod.titolo_sotto_seniority("Addetto HR junior", "0-2"), False)
    check("un titolo senior non viene scartato",
          funnel_mod.titolo_sotto_seniority("Senior HR Business Partner", "5-10"), False)
    check("stage inglese nel senso di fase non è tirocinio",
          funnel_mod.titolo_sotto_seniority("Early Stage HR Consultant", "5-10", "en"),
          False)
    check("stageur è riconosciuto anche senza lingua dichiarata",
          funnel_mod.titolo_sotto_seniority("Stageur Sales Account", "5-10"), True)
    check("l'assenza esplicita di esperienza scarta dai requisiti",
          funnel_mod.titolo_sotto_seniority(
              "Addetto HR", "5-10", requisiti="Anche senza esperienza pregressa"), True)
    check("una frase che richiede esperienza non viene invertita",
          funnel_mod.titolo_sotto_seniority(
              "HR Manager", "5-10",
              requisiti="Non valutiamo candidati senza esperienza pregressa"), False)
    check("requisiti senior non attivano il filtro",
          funnel_mod.titolo_sotto_seniority(
              "HR Manager", "5-10", requisiti="Almeno cinque anni di esperienza"), False)
    check("le occupazioni ESCO HR non finiscono in altre famiglie",
          [tassonomie_mod.famiglia_da_isco(c) for c in ("1212", "2423", "2424", "4416")],
          ["Human Resources"] * 4)
    check("il gruppo ISCO misto 1219 non diventa tutto Security",
          tassonomie_mod.famiglia_da_isco("1219"), "Management & Leadership")

    primo = ModelloFinto("groq", "groq-malformato", [{"reason": "manca"}])
    secondo = ModelloFinto("maestro-locale", "locale-zero",
                           [{"score": 0, "reason": "no"}])
    valutatore = valutatore_da([primo, secondo])
    score, _, _ = valutatore.valuta(profilo, offerta)
    check("score malformato passa davvero al modello successivo",
          (score, secondo.chiuso, primo.chiuso), (0, False, True))
    check("entrambe le chiamate hanno provider e modello reali",
          sorted((provider, model, op, uso["chiamate"])
                 for (provider, model, op), uso in valutatore.totali.items()),
          [("groq", "groq-malformato", "score", 1),
           ("maestro-locale", "locale-zero", "score", 1)])

    cattiva = {"reason": "Parli cinese", "prova": [1],
               "perche": "Inventato", "pros": ["Parli cinese"],
               "cons": ["Nessuna"], "responsabilita": "Volare",
               "requisiti": "Patente inesistente", "benefit": "Jet privato"}
    buona = {"reason": "Il recruiting combacia", "prova": [1],
             "perche": "Hai recruiting", "pros": ["Recruiting"],
             "cons": ["Da approfondire"], "responsabilita": "Selezione",
             "requisiti": "HR", "benefit": None}
    modello = ModelloFinto("groq", "verifica-analisi", [
        cattiva, {"inventato": True}, buona, {"inventato": False}])
    reason, analisi, _ = llm_mod.motiva_verificata(
        modello, profilo, offerta, None, "Italian", "ripiego")
    check("la seconda risposta sostituisce tutta l'analisi rifiutata",
          (reason, analisi["pros"], analisi["requisiti"]),
          ("Il recruiting combacia", ["Recruiting"], "HR"))
    prompt_verifica = modello.chiamate[1][-1]["content"]
    check("il verificatore legge tutti i campi mostrati e l'offerta",
          all(testo in prompt_verifica for testo in (
              "Parli cinese", "Volare", "Patente inesistente", "OFFERTA COMPLETA")),
          True)
    check("anche la seconda passata riceve `_wants`",
          "Voglio crescere" in modello.chiamate[0][-1]["content"], True)

    modello = ModelloFinto("groq", "rifiuto-finale", [
        cattiva, {"inventato": True}, cattiva, {"inventato": True}])
    reason, analisi, _ = llm_mod.motiva_verificata(
        modello, profilo, offerta, None, "Italian", "ripiego sicuro")
    check("dopo due rifiuti resta solo il ripiego, non l'analisi inventata",
          (reason, analisi), ("ripiego sicuro", {}))

    modello = ModelloFinto("groq", "dettaglio-retry", [
        cattiva, {"inventato": True}, buona, {"inventato": False}])
    analisi = llm_mod.analizza_allineamento(modello, profilo, offerta)
    check("il dettaglio recupera l'analisi al secondo tentativo",
          (analisi["pros"], "AL PRIMO GIRO" in modello.chiamate[2][-1]["content"]),
          (["Recruiting"], True))

    lungo = ("L'offerta richiede solida conoscenza del diritto del lavoro italiano "
             "e gestione operativa del payroll, mentre il tuo profilo punta a ruoli come "
             "HR Operations Manager e HR Business Partner. Il secondo punto completa la frase.")
    analisi_lunga = llm_mod._campi_analisi({
        "perche": lungo, "pros": [], "cons": [lungo],
        "responsabilita": None, "requisiti": None, "benefit": None}, "Italian")
    check("i punti non vengono più troncati a metà parola",
          (analisi_lunga["perche"] == lungo, analisi_lunga["cons"] == [lungo],
           analisi_lunga["rubrica"] == llm_mod.VERSIONE_ANALISI), (True, True, True))

    modello = ModelloFinto("groq", "dettaglio-rifiutato", [
        cattiva, {"inventato": True}, cattiva, {"inventato": True}])
    try:
        llm_mod.analizza_allineamento(modello, profilo, offerta)
    except llm_mod.RispostaModelloNonValida:
        ok("il dettaglio non restituisce analisi rifiutata", "nessun JSON da conservare")
    else:
        bad("il dettaglio non restituisce analisi rifiutata", "analisi accettata")

    offerta_lettera = {
        "id": "lettera-1", "title": "HR Manager", "organization": "Acme",
        "ai_core_responsibilities": (
            "Coordinare la selezione e lo sviluppo delle persone, "
            "collaborando con i manager sui processi HR."),
        "ai_requirements_summary": "Esperienza nel recruiting e conoscenza di Workday.",
    }
    lettera_valida = (
        "Gentile team di Acme,\n\n"
        "il ruolo di HR Manager mi interessa perché unisce selezione, sviluppo delle "
        "persone e collaborazione con i manager. Nel mio percorso ho sviluppato competenze "
        "di recruiting che posso mettere a disposizione per costruire processi di selezione "
        "chiari e coerenti con le esigenze dell’azienda.\n\n"
        "La possibilità di contribuire anche allo sviluppo delle persone rappresenta per me "
        "un elemento di continuità con il lavoro svolto in ambito HR. Partirei dalle priorità "
        "dei manager e dall’ascolto delle esigenze dei team, così da sostenere decisioni "
        "condivise e attività ben coordinate. Porterei un approccio attento ai dati, alla "
        "qualità del confronto e al seguimento delle candidature, senza perdere di vista "
        "l’esperienza delle persone coinvolte.\n\n"
        "Sarei lieto di approfondire in un colloquio le priorità del ruolo e il contributo "
        "che potrei offrire al vostro team.\n\n"
        "Cordiali saluti,\nGiuseppe Ranno")
    modello = ModelloFinto("glm", "lettera-valida", [
        {"lettera": lettera_valida}, {"inventato": False}, {"inventato": False}])
    esito_lettera = lettera_mod.genera_lettera(
        modello, profilo, offerta_lettera, nome="Giuseppe Ranno", lingua="Italian")
    check("lettera italiana specifica accettata dopo il controllo sui fatti",
          (esito_lettera, len(modello.chiamate)), (lettera_valida, 3))

    lettera_inventata = lettera_valida.replace("recruiting", "cinese mandarino")
    modello = ModelloFinto("glm", "lettera-retry", [
        {"lettera": lettera_inventata}, {"inventato": True},
        {"lettera": lettera_valida}, {"inventato": False}, {"inventato": False}])
    esito_lettera = lettera_mod.genera_lettera(
        modello, profilo, offerta_lettera, nome="Giuseppe Ranno", lingua="Italian")
    check("lettera inventata rifiutata e rigenerata al secondo tentativo",
          (esito_lettera == lettera_valida,
           "VERSIONE PRECEDENTE" in modello.chiamate[2][-1]["content"]), (True, True))
    attribuzione_implicita = lettera_valida.replace(
        "Nel mio percorso ho sviluppato competenze",
        "Le attività dell’annuncio trovano riscontro nel mio percorso e nelle competenze")
    modello = ModelloFinto("glm", "lettera-attribuzione-implicita", [
        {"lettera": attribuzione_implicita}, {"inventato": False}, {"inventato": True},
        {"lettera": lettera_valida}, {"inventato": False}, {"inventato": False}])
    esito_lettera = lettera_mod.genera_lettera(
        modello, profilo, offerta_lettera, nome="Giuseppe Ranno", lingua="Italian")
    check("il controllo specifico ferma esperienze implicite non documentate",
          esito_lettera, lettera_valida)
    lettera_mista = lettera_valida.replace(
        "processi di selezione", "processi di continuous improvement")
    check("una lettera italiana con anglicismi evitabili viene rifiutata",
          lettera_mod._lettera_valida(
              lettera_mista, offerta_lettera, "Giuseppe Ranno", "Italian"), False)
    lettera_titolo_lungo = lettera_valida.replace(
        "HR Manager", "Cluster HR Manager").replace("Acme", "Aeroviaggi")
    check("titolo principale e ragione sociale abbreviata restano riconoscibili",
          lettera_mod._lettera_valida(
              lettera_titolo_lungo,
              {"title": "Cluster Hr Manager - Himera & Acacia Resort",
               "organization": "AEROVIAGGI S.P.A."},
              "Giuseppe Ranno", "Italian"), True)
    check("una variante naturale del titolo conserva il riferimento al ruolo",
          lettera_mod._lettera_valida(
              lettera_valida.replace("HR Manager", "Consulente di ricerca e selezione"),
              {"title": "consulente di ricerca e selezione del personale",
               "organization": "Acme"},
              "Giuseppe Ranno", "Italian"), True)
    check("una trasferta non diventa un trasferimento di residenza",
          lettera_mod._lettera_valida(
              lettera_valida.replace("processi di selezione", "trasferimento periodico"),
              offerta_lettera, "Giuseppe Ranno", "Italian"), False)

    modello = ModelloFinto("glm", "lettera-doppio-rifiuto", [
        {"lettera": lettera_inventata}, {"inventato": True},
        {"lettera": lettera_inventata}, {"inventato": True}])
    try:
        lettera_mod.genera_lettera(
            modello, profilo, offerta_lettera, nome="Giuseppe Ranno", lingua="Italian")
    except llm_mod.RispostaModelloNonValida:
        ok("dopo due lettere inventate non esiste un ripiego generico", "errore esplicito")
    else:
        bad("dopo due lettere inventate non esiste un ripiego generico", "lettera accettata")

    profilo_esteso = lettera_mod.profilo_per_lettera(
        {"ruolo": "HR Manager", "competenze": ["recruiting"]},
        {"headline": "HR con esperienza nella selezione",
         "roles": [{"title": "HR Specialist", "employer": "Beta", "period": "2022 — oggi"}]},
        nome="Giuseppe Ranno")
    check("la lettera riceve anche incarichi e headline reali del CV",
          all(testo in profilo_esteso for testo in (
              "Giuseppe Ranno", "HR con esperienza", "HR Specialist", "Beta")), True)

    analisi_colloquio = {
        "pros": [
            "Hai competenza diretta su Workday, strumento centrale dell'offerta "
            "per configurazione, testing e implementazione.",
            "Le tue competenze su Power Platform e Power Automate rafforzano il "
            "filone AI e automazione cross-piattaforma menzionato nell'annuncio.",
        ],
        "cons": [
            "L'offerta enfatizza soluzioni AI-enabled dentro Workday: nel colloquio "
            "potresti approfondire la tua esperienza concreta su module AI e automation.",
        ],
    }
    colloquio_valido = {
        "spunti": [
            "Nel raccontare la tua esperienza con Workday, parti da un caso in cui hai "
            "seguito configurazione e test. Spiega quale problema dovevi risolvere e "
            "quale attività hai svolto personalmente, poi collegala alle esigenze di "
            "automazione del ruolo senza dilungarti in un elenco di funzionalità.",
            "Sull’automazione, scegli un flusso realizzato con Power Automate e ricostruisci "
            "il prima e il dopo: cosa richiedeva lavoro manuale, cosa hai configurato e "
            "quale controllo hai aggiunto. È il modo più concreto per dimostrare che puoi "
            "contribuire alla trasformazione tecnologica richiesta dalla posizione.",
        ],
        "domande": [
            {
                "domanda": "Come hai gestito un’automazione in Workday e quali controlli hai fatto prima del rilascio?",
                "preparazione": (
                    "Seleziona un progetto reale e annota il modulo coinvolto, il problema "
                    "iniziale e il tuo ruolo fra configurazione e test. Prepara anche un "
                    "dettaglio tecnico che puoi spiegare con sicurezza e un esito che puoi documentare."),
                "risposta": (
                    "Comincia dal problema, poi descrivi le attività svolte direttamente e "
                    "i controlli prima del rilascio. Distingui il tuo contributo da quello "
                    "del team e chiudi sull’esito osservato, senza attribuirti funzionalità "
                    "che hai soltanto visto utilizzare da altri."),
            },
            {
                "domanda": "Come affronteresti funzionalità di intelligenza artificiale che non hai ancora usato in produzione?",
                "preparazione": (
                    "Distingui ciò che conosci già da ciò che dovresti imparare. Prepara "
                    "un caso di automazione affine e un piano di apprendimento sul modulo "
                    "specifico, con un criterio concreto per verificarne affidabilità e risultati."),
                "risposta": (
                    "Dichiara subito il limite senza trasformarlo in un punto di forza. "
                    "Poi porta l’esempio di automazione più vicino, spiega quali basi ti "
                    "fornisce e indica come valuteresti il nuovo strumento su un caso pilota "
                    "prima di utilizzarlo nei processi HR."),
            },
        ],
    }
    modello = ModelloFinto("glm", "colloquio-valido", [
        colloquio_valido, {"inventato": False}])
    esito_colloquio = colloquio_mod.genera_colloquio(
        modello, profilo, offerta_lettera, analisi=analisi_colloquio, lingua="Italian")
    check("spunti e domande del colloquio sono discorsivi e verificati",
          (esito_colloquio, len(modello.chiamate)), (colloquio_valido, 2))
    colloquio_misto = dict(colloquio_valido)
    colloquio_misto["spunti"] = [colloquio_valido["spunti"][0] + " hands-on",
                                 colloquio_valido["spunti"][1]]
    check("il colloquio rifiuta anglicismi evitabili",
          colloquio_mod._errore_contenuto(
              colloquio_misto, "Italian", analisi_colloquio) is not None, True)
    colloquio_articolo = dict(colloquio_valido)
    colloquio_articolo["spunti"] = [colloquio_valido["spunti"][0] + " del impatto",
                                    colloquio_valido["spunti"][1]]
    check("il colloquio rifiuta anche errori d’articolo",
          colloquio_mod._errore_contenuto(
              colloquio_articolo, "Italian", analisi_colloquio) is not None, True)

    colloquio_copiato = dict(colloquio_valido)
    colloquio_copiato["spunti"] = [analisi_colloquio["pros"][0],
                                   colloquio_valido["spunti"][1]]
    modello = ModelloFinto("glm", "colloquio-copia", [
        colloquio_copiato, colloquio_valido, {"inventato": False}])
    esito_colloquio = colloquio_mod.genera_colloquio(
        modello, profilo, offerta_lettera, analisi=analisi_colloquio, lingua="Italian")
    check("il copia e incolla dell’analisi viene rifiutato e riscritto",
          esito_colloquio, colloquio_valido)

    modello = ModelloFinto("glm", "colloquio-inventato", [
        colloquio_valido, {"inventato": True},
        colloquio_valido, {"inventato": False}])
    esito_colloquio = colloquio_mod.genera_colloquio(
        modello, profilo, offerta_lettera, analisi=analisi_colloquio, lingua="Italian")
    check("il colloquio non attribuisce esperienze non documentate",
          esito_colloquio, colloquio_valido)

    modello = ModelloFinto("glm", "colloquio-grammatica", [
        colloquio_valido, {"inventato": False, "forma_scorretta": True},
        colloquio_valido, {"inventato": False, "forma_scorretta": False}])
    esito_colloquio = colloquio_mod.genera_colloquio(
        modello, profilo, offerta_lettera, analisi=analisi_colloquio, lingua="Italian")
    check("refusi e frasi incomplete vengono rifiutati e riscritti",
          esito_colloquio, colloquio_valido)

    modello = ModelloFinto("glm", "colloquio-doppio-rifiuto", [
        colloquio_valido, {"inventato": True},
        colloquio_valido, {"inventato": True}])
    try:
        colloquio_mod.genera_colloquio(
            modello, profilo, offerta_lettera, analisi=analisi_colloquio, lingua="Italian")
    except llm_mod.RispostaModelloNonValida:
        ok("dopo due contenuti colloquio inventati non c’è un ripiego standard", "errore esplicito")
    else:
        bad("dopo due contenuti colloquio inventati non c’è un ripiego standard", "contenuto accettato")

    conn = mock.MagicMock()
    conn.__enter__.return_value = conn
    with (mock.patch.object(worker.psycopg, "connect", return_value=conn),
          mock.patch.object(worker, "load_dotenv"),
          mock.patch.object(worker, "database_url", return_value="postgresql:///dry-test"),
          mock.patch.object(worker, "safe_dsn", side_effect=lambda dsn: dsn),
          mock.patch.object(worker, "utenti_dovuti",
                            return_value=[mock.Mock(email="dry@example.test",
                                                    next_digest_at=None,
                                                    ricerche_dovute=[])]),
          mock.patch.object(worker, "digest_utente",
                            side_effect=RuntimeError("modello finto assente")),
          mock.patch.object(worker, "_segna_fallito") as segna):
        worker.main(["--dry-run"])
        check("un errore nel main dry-run non marca il digest né lo slot",
              segna.call_count, 0)
        worker.main([])
        check("lo stesso errore nel giro normale resta registrato e riprendibile",
              segna.call_count, 1)


def check_frequenza_per_ricerca(work, seen_by_other) -> None:
    """I dodici confini dello scheduling per ricerca, senza provider reali."""
    section("frequenza per ricerca: eredita', override, slot e retry")
    wipe(work)
    ids: dict[str, str] = {}
    sids: list[str] = []
    with work.cursor() as cur:
        for chiave, paese in (("a", "IT"), ("b", "DE"), ("c", "FR"),
                              ("legacy", "ES"), ("paused", "PT"),
                              ("dormant", "SE")):
            cur.execute("INSERT INTO clusters (family, country) "
                        "VALUES ('Human Resources', %s) RETURNING id", (paese,))
            ids[chiave] = str(cur.fetchone()[0])
        cur.execute("UPDATE clusters SET status = 'paused' WHERE id = %s",
                    (ids["dormant"],))
        cur.execute(
            "INSERT INTO users (email, plan, subscription_status, delivery_channels, "
            "frequency, send_weekday, send_hour_local, timezone, next_digest_at) "
            "VALUES ('frequency-search@example.test', 'pro', 'active', '{email}', "
            "'weekly', 1, 8, 'Europe/Rome', now() + interval '10 days') RETURNING id")
        uid = str(cur.fetchone()[0])
        ids["user"] = uid
        cur.execute(
            "INSERT INTO user_cvs (user_id, storage_key, families, seniority, skills, "
            "languages, years_experience, encryption_algo, encrypted_dek, nonce, "
            "auth_tag, kek_version) VALUES (%s, 'cv/frequency-search.pdf', "
            "ARRAY['Human Resources'], '5-10', ARRAY['recruiting'], "
            "'[\"Italian\"]'::jsonb, 8, 'aes-256-gcm', decode(repeat('ab',32),'hex'), "
            "decode(repeat('cd',12),'hex'), decode(repeat('ef',16),'hex'), 1)", (uid,))
        for chiave, ruolo, frequency, weekday, monthday in (
                ("a", "HR Daily", "daily", None, None),
                ("b", "HR Weekly", "weekly", 2, None),
                ("c", "HR Monthly", "monthly", None, 15),
                ("legacy", "HR Inherited", None, None, None),
                ("paused", "HR Paused", "daily", None, None),
                ("dormant", "HR Dormant", "daily", None, None)):
            cur.execute(
                "INSERT INTO user_clusters (user_id, cluster_id, target_role, frequency, "
                "send_weekday, send_monthday, next_digest_at) VALUES "
                "(%s, %s, %s, %s, %s, %s, CASE WHEN %s::text IS NULL THEN NULL "
                "ELSE now() + interval '20 days' END) RETURNING id",
                (uid, ids[chiave], ruolo, frequency, weekday, monthday, frequency))
            ids[chiave + "_search"] = str(cur.fetchone()[0])
        cur.execute("UPDATE user_clusters SET is_paused = true, "
                    "next_digest_at = TIMESTAMPTZ '2020-01-01 00:00+00' "
                    "WHERE id = %s", (ids["paused_search"],))
        cur.execute("UPDATE user_clusters SET "
                    "next_digest_at = TIMESTAMPTZ '2020-01-01 00:00+00' "
                    "WHERE id = %s", (ids["dormant_search"],))

        def offerta(chiave, cluster):
            sid = "frequency-" + chiave
            sids.append(sid)
            cur.execute(
                "INSERT INTO jobs (source, source_job_id, url, canonical_url, "
                "domain_derived, title, title_normalized, organization, date_posted, "
                "countries, ai_job_language, ai_experience_level, ai_key_skills, "
                "ai_requirements_summary, ai_core_responsibilities, raw, link_kind) "
                "VALUES ('fantastic', %s, %s, %s, 'acme.example', %s, %s, 'Acme', "
                "now(), ARRAY['IT'], 'Italian', '5-10', ARRAY['recruiting'], "
                "'requisiti', 'responsabilita', '{}'::jsonb, 'career_site') RETURNING id",
                (sid, "https://acme.example/" + sid, "https://acme.example/" + sid,
                 chiave, chiave.lower()))
            jid = str(cur.fetchone()[0])
            ids[chiave] = jid
            cur.execute("INSERT INTO job_clusters (job_id, cluster_id) VALUES (%s, %s)",
                        (jid, ids[cluster]))
            return jid

        offerta("daily-job", "a")
        offerta("weekly-job", "b")
        offerta("inherited-job", "legacy")
        cur.execute("UPDATE user_clusters SET next_digest_at = now() - interval '2 hours' "
                    "WHERE id = %s", (ids["a_search"],))
        cur.execute("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                    (ids["a_search"],))
        slot_a = cur.fetchone()[0]
        cur.execute("SELECT next_digest_at FROM users WHERE id = %s", (uid,))
        slot_globale_iniziale = cur.fetchone()[0]
    work.commit()

    from psycopg.rows import dict_row

    def carica():
        with work.cursor(row_factory=dict_row) as cur:
            return next(u for u in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                        if u.id == uid)

    def valutatore():
        return ValutatoreFinto({sid: 90 for sid in sids})

    invii: list[str] = []
    originale_invia = worker.email_mod.invia
    originale_anteprima = worker.email_mod.anteprima
    originale_prescore = worker.prescore_mod.disponibile
    originale_push = worker.push_mod.invia_digest_pronto
    worker.email_mod.invia = lambda dest, items, locale: (
        invii.append(dest) or "<frequency@test>")
    worker.email_mod.anteprima = lambda *a, **kw: "<frequency-dry>"
    worker.prescore_mod.disponibile = lambda: False
    worker.push_mod.invia_digest_pronto = lambda *a, **kw: 0
    try:
        u = carica()
        check("override giornaliero dovuto anche con scadenza globale futura",
              ([r.id for r in u.ricerche_dovute], worker._slot_digest(
                  u, datetime.now(timezone.utc))),
              ([ids["a_search"]], slot_a))
        with work.cursor(row_factory=dict_row) as cur:
            check("nessuna scadenza: nessun filtro valutabile",
                  funnel_mod._filtri(cur, uid, [], False), [])
            check("solo l'override dovuto entra nel funnel",
                  {f["ricerca_id"] for f in funnel_mod._filtri(
                      cur, uid, [ids["a_search"]], False)}, {ids["a_search"]})
        candidati_a = funnel_mod.candidati(work, uid, [ids["a_search"]], False)
        check("ricerche non dovute non vengono valutate",
              [j["source_job_id"] for j in candidati_a], ["frequency-daily-job"])
        check("righe legacy senza override conservano tutti i campi NULL",
              seen_by_other(
                  "SELECT frequency IS NULL AND send_weekday IS NULL "
                  "AND send_monthday IS NULL AND send_hour_local IS NULL "
                  "AND timezone IS NULL AND last_digest_at IS NULL "
                  "FROM user_clusters WHERE id = %s", (ids["legacy_search"],)), True)

        e = worker.digest_utente(work, u, dry_run=True, evaluatore=valutatore())
        check("dry-run override compila lo slot senza consegnare",
              (e["stato"], e["slot"], e["valutate"], e["inviate"]),
              ("dry_run", slot_a, 1, 1))
        check("dry-run override non lascia digest o match",
              (seen_by_other("SELECT count(*) FROM digests WHERE user_id = %s", (uid,)),
               seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s", (uid,))),
              (0, 0))
        check("dry-run override non muove scadenze globali o specifiche",
              (seen_by_other("SELECT next_digest_at FROM users WHERE id = %s", (uid,)),
               seen_by_other("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],))),
              (slot_globale_iniziale, slot_a))

        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("primo override inviato nello slot specifico",
              (e["stato"], e["slot"], e["inviate"]), ("sent", slot_a, 1))
        check("override consegnato non anticipa la scadenza globale non dovuta",
              seen_by_other("SELECT next_digest_at FROM users WHERE id = %s", (uid,)),
              slot_globale_iniziale)
        check("solo la ricerca daily ha prodotto una valutazione",
              seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s", (uid,)), 1)
        check("last_digest_at attribuito all'UUID daily davvero incluso",
              seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                            (ids["a_search"],)), slot_a)
        check("la ricerca weekly non dovuta non e' stata marcata consegnata",
              seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                            (ids["b_search"],)), None)
        check("scadenza globale futura non attiva eredita_dovuta",
              u.eredita_dovuta, False)
        check("override in pausa con next vecchio non selezionato né rischedulato",
              (ids["paused_search"] not in [r.id for r in u.ricerche_dovute],
               seen_by_other(
                   "SELECT next_digest_at = TIMESTAMPTZ '2020-01-01 00:00+00' "
                   "AND last_digest_at IS NULL FROM user_clusters WHERE id = %s",
                   (ids["paused_search"],))), (True, True))
        check("override con cluster inattivo non selezionato né rischedulato",
              (ids["dormant_search"] not in [r.id for r in u.ricerche_dovute],
               seen_by_other(
                   "SELECT next_digest_at = TIMESTAMPTZ '2020-01-01 00:00+00' "
                   "AND last_digest_at IS NULL FROM user_clusters WHERE id = %s",
                   (ids["dormant_search"],))), (True, True))
        e_retry = worker.digest_utente(work, u, evaluatore=valutatore())
        check("retry dello stesso slot override e' idempotente",
              (e_retry["stato"], len(invii),
               seen_by_other("SELECT count(*) FROM digest_items di "
                             "JOIN digests d ON d.id = di.digest_id WHERE d.user_id = %s",
                             (uid,))), ("già consegnato", 1, 1))
        u.next_digest_at = datetime.now(timezone.utc) + timedelta(days=2)
        u.ricerche_dovute = []
        check("senza scadenze effettive non parte alcuna valutazione",
              worker.digest_utente(work, u, evaluatore=valutatore())["stato"],
              "non_dovuto")

        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET next_digest_at = now() - interval '90 minutes' "
                        "WHERE id = %s", (ids["b_search"],))
            cur.execute("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                        (ids["b_search"],))
            slot_b = cur.fetchone()[0]
        work.commit()
        u = carica()
        started_b = datetime.now(timezone.utc)
        digest_b = worker._digest_row(work, u, started_b, slot_b)
        worker._segna_fallito(work, u, "errore sintetico override", slot_b)
        check("_digest_row e _segna_fallito usano lo slot override, non quello globale",
              seen_by_other("SELECT status FROM digests WHERE id = %s", (digest_b,)),
              "failed")
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("weekly entra in uno slot diverso e il retry riprende il failed",
              (e["stato"], e["slot"],
               seen_by_other("SELECT attempt_count FROM digests WHERE id = %s",
                             (digest_b,))), ("sent", slot_b, 2))
        check("daily e weekly hanno digest e item distinti, senza doppie consegne",
              (seen_by_other("SELECT count(*) FROM digests WHERE user_id = %s", (uid,)),
               seen_by_other("SELECT count(*) FROM digest_items di "
                             "JOIN digests d ON d.id = di.digest_id WHERE d.user_id = %s",
                             (uid,))), (2, 2))

        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = true WHERE id = %s",
                        (ids["legacy_search"],))
            cur.execute("UPDATE users SET next_digest_at = now() - interval '30 days' "
                        "WHERE id = %s", (uid,))
        work.commit()
        with work.cursor(row_factory=dict_row) as cur:
            check("globale vecchio senza ereditata attiva non produce uno slot vuoto",
                  [u.id for u in worker.utenti_dovuti(
                      cur, datetime.now(timezone.utc))], [])
        with work.cursor() as cur:
            cur.execute("SELECT next_digest_at FROM users WHERE id = %s", (uid,))
            globale_senza_ereditata = cur.fetchone()[0]
            cur.execute("UPDATE user_clusters SET next_digest_at = now() - interval '30 minutes' "
                        "WHERE id = %s", (ids["c_search"],))
            cur.execute("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                        (ids["c_search"],))
            slot_c = cur.fetchone()[0]
        work.commit()
        u = carica()
        check("solo override attivo: lo slot specifico precede il globale vecchio ignorato",
              (u.eredita_dovuta, worker._slot_digest(u, datetime.now(timezone.utc))),
              (False, slot_c))
        prima_vuoto = datetime.now(timezone.utc)
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        prossimo_c = seen_by_other(
            "SELECT next_digest_at FROM user_clusters WHERE id = %s", (ids["c_search"],))
        check("monthly senza elementi e' skipped_empty",
              (e["stato"], e["inviate"]), ("skipped_empty", 0))
        check("rischedula override non avanza il globale senza ereditata attiva",
              seen_by_other("SELECT next_digest_at FROM users WHERE id = %s", (uid,)),
              globale_senza_ereditata)
        check("primo vuoto monthly riprova il giorno dopo, non fra un mese",
              prima_vuoto + timedelta(hours=23, minutes=55) <= prossimo_c
              <= prima_vuoto + timedelta(days=1, minutes=5), True)
        check("ricerca senza elementi propri non viene marcata consegnata",
              seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                            (ids["c_search"],)), None)
        check("le ricerche con elementi conservano elementi e cronologia",
              (seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["b_search"],)),
               seen_by_other("SELECT count(*) FROM digest_items di "
                             "JOIN digests d ON d.id = di.digest_id WHERE d.user_id = %s",
                             (uid,))), (slot_a, slot_b, 2))

        with work.cursor() as cur:
            offerta("shared-job", "a")
            cur.execute("INSERT INTO job_clusters (job_id, cluster_id) VALUES (%s, %s)",
                        (ids["shared-job"], ids["b"]))
            slot_combo = datetime.now(timezone.utc) - timedelta(minutes=37)
            for chiave in ("a_search", "b_search"):
                cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id = %s",
                            (slot_combo, ids[chiave]))
        work.commit()
        u = carica()
        candidati_combo = funnel_mod.candidati(
            work, uid, [ids["a_search"], ids["b_search"]], False)
        check("slot combinato deduplica il candidato e conserva entrambi gli UUID",
              ([j["source_job_id"] for j in candidati_combo],
               set(candidati_combo[0]["_ricerca_ids"])),
              (["frequency-shared-job"], {ids["a_search"], ids["b_search"]}))
        finto_combo = valutatore()
        e = worker.digest_utente(work, u, evaluatore=finto_combo)
        check("slot combinato invia una sola offerta con una sola valutazione",
              (e["stato"], e["valutate"], e["inviate"],
               finto_combo.totale["chiamate"]), ("sent", 1, 1, 2))
        check("slot combinato crea un solo digest_item per la stessa offerta",
              seen_by_other("SELECT count(*) FROM digest_items di "
                            "JOIN digests d ON d.id = di.digest_id "
                            "WHERE d.user_id = %s AND di.job_id = %s",
                            (uid, ids["shared-job"])), 1)
        check("offerta condivisa attribuita a entrambe le ricerche incluse",
              (seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["b_search"],))), (slot_combo, slot_combo))
        check("monthly senza item resta fuori dal conteggio delle consegne",
              seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                            (ids["c_search"],)), None)
        check("retry dello slot combinato non duplica consegne o valutazioni",
              (worker.digest_utente(work, u, evaluatore=valutatore())["stato"],
               len(invii), seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                                         (uid,))), ("già consegnato", 3, 3))
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = true, min_score = 95 "
                        "WHERE id = %s", (ids["b_search"],))
        work.commit()
        check("pausa e cambio soglia non cancellano last o storico dello slot",
              (seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["b_search"],)),
               seen_by_other("SELECT count(*) FROM digest_items di "
                             "JOIN digests d ON d.id = di.digest_id WHERE d.user_id = %s",
                             (uid,))), (slot_combo, 3))
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = false, min_score = NULL "
                        "WHERE id = %s", (ids["b_search"],))

        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = false WHERE id = %s",
                        (ids["legacy_search"],))
            slot_globale = datetime.now(timezone.utc) - timedelta(minutes=20)
            slot_a_extra = datetime.now(timezone.utc) - timedelta(minutes=25)
            cur.execute("UPDATE users SET next_digest_at = %s WHERE id = %s",
                        (slot_globale, uid))
            cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id = %s",
                        (slot_a_extra, ids["a_search"]))
        work.commit()
        u = carica()
        check("eredita_dovuta attiva solo con ereditata attiva e globale dovuta",
              u.eredita_dovuta, True)
        with work.cursor(row_factory=dict_row) as cur:
            check("slot globale include ereditate e override dovuti, mai i non dovuti",
                  {f["ricerca_id"] for f in funnel_mod._filtri(
                      cur, uid, [ids["a_search"]], True)},
                  {ids["legacy_search"], ids["a_search"]})
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("prima scadenza effettiva scelta fra globale e override",
              (e["stato"], e["slot"]), ("sent", slot_a_extra))
        check("contenuto ereditato aggiorna last globale e della ricerca legacy",
              (seen_by_other("SELECT last_digest_at FROM users WHERE id = %s", (uid,)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["legacy_search"],))), (slot_a_extra, slot_a_extra))
        check("override senza elementi nello slot globale conserva il proprio last",
              seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                            (ids["a_search"],)), slot_combo)

        prima_dst = datetime(2026, 3, 28, 23, 30, tzinfo=timezone.utc)
        check("calcola_slot resta la fonte unica per DST primavera",
              worker.calcola_slot("daily", 7, None, None, "Europe/Rome", prima_dst),
              datetime(2026, 3, 29, 5, 0, tzinfo=timezone.utc))
        autunno_dst = datetime(2026, 10, 24, 23, 30, tzinfo=timezone.utc)
        check("calcola_slot resta la fonte unica per DST autunno",
              worker.calcola_slot("daily", 7, None, None, "Europe/Rome", autunno_dst),
              datetime(2026, 10, 25, 6, 0, tzinfo=timezone.utc))
    finally:
        worker.email_mod.invia = originale_invia
        worker.email_mod.anteprima = originale_anteprima
        worker.prescore_mod.disponibile = originale_prescore
        worker.push_mod.invia_digest_pronto = originale_push


def check_recupero_e_race(work, seen_by_other) -> None:
    section("crash, recupero consegne e race di scheduling")
    wipe(work)
    ids: dict[str, str] = {}
    sids: list[str] = []
    with work.cursor() as cur:
        for chiave, paese in (("a", "IT"), ("b", "DE"), ("legacy", "FR")):
            cur.execute("INSERT INTO clusters (family, country) "
                        "VALUES ('Human Resources', %s) RETURNING id", (paese,))
            ids[chiave] = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO users (email, plan, subscription_status, delivery_channels, "
            "frequency, send_hour_local, timezone) VALUES "
            "('recovery@example.test', 'pro', 'active', '{email}', 'daily', 8, 'UTC') "
            "RETURNING id")
        uid = str(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO user_cvs (user_id, storage_key, families, seniority, skills, "
            "encryption_algo, encrypted_dek, nonce, auth_tag, kek_version) VALUES "
            "(%s, 'cv/recovery.pdf', ARRAY['Human Resources'], '5-10', "
            "ARRAY['recruiting'], 'aes-256-gcm', decode(repeat('ab',32),'hex'), "
            "decode(repeat('cd',12),'hex'), decode(repeat('ef',16),'hex'), 1)", (uid,))
        for chiave, ruolo, frequency, monthday in (
                ("a", "Recovery A", "daily", None),
                ("b", "Recovery B", "monthly", 15),
                ("legacy", "Recovery inherited", None, None)):
            cur.execute(
                "INSERT INTO user_clusters (user_id, cluster_id, target_role, frequency, "
                "send_monthday) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (uid, ids[chiave], ruolo, frequency, monthday))
            ids[chiave + "_search"] = str(cur.fetchone()[0])

        def offerta(chiave, cluster):
            sid = "recovery-" + chiave
            sids.append(sid)
            cur.execute(
                "INSERT INTO jobs (source, source_job_id, url, canonical_url, "
                "domain_derived, title, title_normalized, organization, date_posted, "
                "countries, ai_job_language, ai_experience_level, raw, link_kind) "
                "VALUES ('fantastic', %s, %s, %s, 'acme.example', %s, %s, 'Acme', "
                "now(), ARRAY['IT'], 'Italian', '5-10', '{}'::jsonb, 'career_site') "
                "RETURNING id",
                (sid, "https://acme.example/" + sid, "https://acme.example/" + sid,
                 chiave, chiave.lower()))
            jid = str(cur.fetchone()[0])
            ids[chiave] = jid
            cur.execute("INSERT INTO job_clusters (job_id, cluster_id) VALUES (%s, %s)",
                        (jid, ids[cluster]))

        offerta("a1", "a")
        slot_a = datetime.now(timezone.utc) - timedelta(hours=2)
        slot_b = datetime.now(timezone.utc) - timedelta(minutes=90)
        cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id = %s",
                    (slot_a, ids["a_search"]))
        cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id = %s",
                    (slot_b, ids["b_search"]))
    work.commit()

    from psycopg.rows import dict_row

    def carica():
        with work.cursor(row_factory=dict_row) as cur:
            return next(u for u in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                        if u.id == uid)

    def valutatore():
        return ValutatoreFinto({sid: 90 for sid in sids})

    def digest_id(slot):
        return seen_by_other(
            "SELECT id::text FROM digests WHERE user_id = %s AND scheduled_for = %s",
            (uid, slot))

    invii: list[str] = []
    originale_invia = worker.email_mod.invia
    originale_anteprima = worker.email_mod.anteprima
    originale_prescore = worker.prescore_mod.disponibile
    originale_push = worker.push_mod.invia_digest_pronto
    originale_rischedula = worker._rischedula
    worker.email_mod.invia = lambda dest, items, locale: invii.append(dest) or "<recovery@test>"
    worker.email_mod.anteprima = lambda *a, **kw: "<recovery-dry>"
    worker.prescore_mod.disponibile = lambda: False
    worker.push_mod.invia_digest_pronto = lambda *a, **kw: 0
    try:
        u = carica()

        def crash_dopo_chiusura(*args, **kwargs):
            raise RuntimeError("crash sintetico dopo chiusura")

        worker._rischedula = crash_dopo_chiusura
        try:
            worker.digest_utente(work, u, evaluatore=valutatore())
            bad("crash dopo chiusura deve sollevare", "non ha sollevato")
        except RuntimeError as exc:
            check("crash fra chiusura e rischedulazione intercettato",
                  str(exc), "crash sintetico dopo chiusura")
        finally:
            worker._rischedula = originale_rischedula
            work.rollback()

        did_a = digest_id(slot_a)
        check("chiusura e rischedulazione non lasciano sent senza slot: rollback atomico",
              (seen_by_other("SELECT status FROM digests WHERE id = %s", (did_a,)),
               seen_by_other("SELECT count(*) FROM digest_items WHERE digest_id = %s",
                             (did_a,)),
               seen_by_other("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],))),
              ("pending", 1, slot_a))
        check("crash prima della chiusura lascia anche last e next dell'altra ricerca invariati",
              seen_by_other(
                  "SELECT last_digest_at IS NULL AND next_digest_at = %s "
                  "FROM user_clusters WHERE id = %s", (slot_b, ids["b_search"])), True)

        e = worker.digest_utente(work, u, dry_run=True, evaluatore=valutatore())
        check("recovery con item in dry-run non consegna e non committa",
              (e["stato"], len(invii),
               seen_by_other("SELECT status FROM digests WHERE id = %s", (did_a,)),
               seen_by_other("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],))),
              ("già consegnato", 1, "pending", slot_a))

        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("pending con item e zero extra: chiude sent senza rispedire",
              (e["stato"], len(invii),
               seen_by_other("SELECT status || ':' || jobs_evaluated_count || ':' || "
                             "jobs_sent_count FROM digests WHERE id = %s", (did_a,))),
              ("già consegnato", 1, "sent:1:1"))
        check("recovery conserva un solo item e il rank originale",
              seen_by_other("SELECT count(*) || ':' || min(rank) || ':' || max(rank) "
                            "FROM digest_items WHERE digest_id = %s", (did_a,)),
              "1:1:1")
        check("attribuzione recovery: A consegnata, B senza elementi non marcata",
              (seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["b_search"],))), (slot_a, None))
        next_a = seen_by_other(
            "SELECT next_digest_at FROM user_clusters WHERE id = %s", (ids["a_search"],))
        next_b = seen_by_other(
            "SELECT next_digest_at FROM user_clusters WHERE id = %s", (ids["b_search"],))

        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id IN (%s, %s)",
                        (slot_a, ids["a_search"], ids["b_search"]))
        work.commit()
        u = carica()
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("terminale sent dopo chiusura riprende lo slot senza duplicare",
              (e["stato"], len(invii),
               seen_by_other("SELECT count(*) FROM digest_items WHERE digest_id = %s",
                             (did_a,))), ("già consegnato", 1, 1))
        next_a_terminal = seen_by_other(
            "SELECT next_digest_at FROM user_clusters WHERE id = %s", (ids["a_search"],))
        next_b_terminal = seen_by_other(
            "SELECT next_digest_at FROM user_clusters WHERE id = %s", (ids["b_search"],))
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("retry stale non sposta di nuovo next già avanzato",
              (e["stato"],
               seen_by_other("SELECT next_digest_at = %s FROM user_clusters WHERE id = %s",
                             (next_a_terminal, ids["a_search"])),
               seen_by_other("SELECT next_digest_at = %s FROM user_clusters WHERE id = %s",
                             (next_b_terminal, ids["b_search"]))),
              ("già consegnato", True, True))
        check("next calcolati nel recovery restano coerenti",
              (next_a != slot_a, next_b != slot_a), (True, True))

        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = true, next_digest_at = %s "
                        "WHERE id IN (%s, %s)", (slot_a, ids["a_search"], ids["b_search"]))
        work.commit()
        with work.cursor(row_factory=dict_row) as cur:
            check("ricerche in pausa sullo slot chiuso non entrano nel worker",
                  [x.id for x in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                   if x.id == uid], [])
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = false WHERE id IN (%s, %s)",
                        (ids["a_search"], ids["b_search"]))
        work.commit()
        u = carica()
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("riattivazione su slot chiuso: avanza senza rispedire",
              (e["stato"], len(invii),
               seen_by_other("SELECT count(*) FROM digest_items WHERE digest_id = %s",
                             (did_a,))), ("già consegnato", 1, 1))
        check("riattivazione conserva last di A e non inventa quello di B",
              (seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["b_search"],))), (slot_a, None))

        with work.cursor() as cur:
            cur.execute("UPDATE digests SET status = 'skipped_empty', sent_at = NULL, "
                        "jobs_sent_count = 0 WHERE id = %s", (did_a,))
            cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id IN (%s, %s)",
                        (slot_a, ids["a_search"], ids["b_search"]))
        work.commit()
        u = carica()
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("skipped_empty con item legacy ripara in sent senza errore UNIQUE o rinvio",
              (e["stato"], len(invii),
               seen_by_other("SELECT status || ':' || jobs_sent_count FROM digests "
                             "WHERE id = %s", (did_a,)),
               seen_by_other("SELECT count(*) FROM digest_items WHERE digest_id = %s",
                             (did_a,))), ("già consegnato", 1, "sent:1", 1))

        with work.cursor() as cur:
            offerta("extra", "a")
            cur.execute("UPDATE digests SET status = 'pending', sent_at = NULL WHERE id = %s",
                        (did_a,))
            cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id IN (%s, %s)",
                        (slot_a, ids["a_search"], ids["b_search"]))
        work.commit()
        u = carica()
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("pending con item e offerta extra: l'extra resta al prossimo slot",
              (e["stato"], len(invii),
               seen_by_other("SELECT count(*) FROM matches m JOIN jobs j ON j.id = m.job_id "
                             "WHERE m.user_id = %s AND j.source_job_id = 'recovery-extra'",
                             (uid,)),
               seen_by_other("SELECT count(*) FROM digest_items WHERE digest_id = %s",
                             (did_a,))), ("già consegnato", 1, 0, 1))
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET next_digest_at = now() - interval '1 minute' "
                        "WHERE id = %s", (ids["a_search"],))
            cur.execute("UPDATE user_clusters SET next_digest_at = now() + interval '10 days' "
                        "WHERE id = %s", (ids["b_search"],))
        work.commit()
        u = carica()
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("allo slot successivo parte soltanto l'extra, con un nuovo rank 1",
              (e["stato"], e["inviate"], len(invii),
               seen_by_other("SELECT di.rank FROM digest_items di "
                             "JOIN jobs j ON j.id = di.job_id "
                             "WHERE di.user_id = %s AND j.source_job_id = 'recovery-extra'",
                             (uid,))), ("sent", 1, 2, 1))
        check("l'item originario non e' stato duplicato in nessun digest",
              seen_by_other("SELECT count(*) FROM digest_items di "
                            "JOIN jobs j ON j.id = di.job_id "
                            "WHERE di.user_id = %s AND j.source_job_id = 'recovery-a1'",
                            (uid,)), 1)

        slot_empty = datetime.now(timezone.utc) - timedelta(minutes=5)
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET next_digest_at = now() + interval '10 days' "
                        "WHERE id = %s", (ids["a_search"],))
            cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id = %s",
                        (slot_empty, ids["b_search"]))
        work.commit()
        u = carica()
        worker._rischedula = crash_dopo_chiusura
        try:
            worker.digest_utente(work, u, evaluatore=valutatore())
            bad("crash skipped_empty deve sollevare", "non ha sollevato")
        except RuntimeError:
            ok("crash dopo chiusura skipped_empty", "eccezione simulata")
        finally:
            worker._rischedula = originale_rischedula
            work.rollback()
        did_empty = digest_id(slot_empty)
        check("skipped_empty e rischedulazione rollback insieme",
              (seen_by_other("SELECT status FROM digests WHERE id = %s", (did_empty,)),
               seen_by_other("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                             (ids["b_search"],))), ("pending", slot_empty))
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("retry senza item segue il percorso normale skipped_empty",
              (e["stato"], len(invii),
               seen_by_other("SELECT status FROM digests WHERE id = %s", (did_empty,))),
              ("skipped_empty", 2, "skipped_empty"))
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id = %s",
                        (slot_empty, ids["b_search"]))
        work.commit()
        u = carica()
        e = worker.digest_utente(work, u, evaluatore=valutatore())
        check("terminale skipped_empty riprende next senza marcare la ricerca",
              (e["stato"], len(invii),
               seen_by_other("SELECT next_digest_at <> %s AND last_digest_at IS NULL "
                             "FROM user_clusters WHERE id = %s",
                             (slot_empty, ids["b_search"]))),
              ("già consegnato", 2, True))

        slot_globale = datetime.now(timezone.utc) - timedelta(minutes=30)
        nuovo_globale = datetime.now(timezone.utc) + timedelta(days=3)
        with work.cursor() as cur:
            cur.execute("UPDATE users SET next_digest_at = %s, last_digest_at = NULL "
                        "WHERE id = %s", (slot_globale, uid))
            cur.execute("UPDATE user_clusters SET next_digest_at = now() + interval '20 days' "
                        "WHERE user_id = %s AND frequency IS NOT NULL", (uid,))
        work.commit()
        u = carica()
        with work.cursor() as cur:
            cur.execute("UPDATE users SET send_hour_local = 23, next_digest_at = %s "
                        "WHERE id = %s", (nuovo_globale, uid))
        work.commit()
        worker._rischedula(work, u, datetime.now(timezone.utc), consegnato=True,
                           scheduled_for=slot_globale,
                           ricerche_consegnate={ids["legacy_search"]})
        check("race globale: last registrata ma il next dell'API non viene sovrascritto",
              (seen_by_other("SELECT last_digest_at FROM users WHERE id = %s", (uid,)),
               seen_by_other("SELECT next_digest_at FROM users WHERE id = %s", (uid,))),
              (slot_globale, nuovo_globale))
        last_piu_recente = slot_globale + timedelta(days=1)
        with work.cursor() as cur:
            cur.execute("UPDATE users SET last_digest_at = %s WHERE id = %s",
                        (last_piu_recente, uid))
            cur.execute("UPDATE user_clusters SET last_digest_at = %s WHERE id = %s",
                        (last_piu_recente, ids["legacy_search"]))
        work.commit()
        worker._rischedula(work, u, datetime.now(timezone.utc), consegnato=True,
                           scheduled_for=slot_globale,
                           ricerche_consegnate={ids["legacy_search"]})
        check("last globale e per ricerca non tornano mai indietro",
              (seen_by_other("SELECT last_digest_at FROM users WHERE id = %s", (uid,)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["legacy_search"],))),
              (last_piu_recente, last_piu_recente))

        slot_override = datetime.now(timezone.utc) - timedelta(minutes=25)
        nuovo_override = datetime.now(timezone.utc) + timedelta(days=4)
        with work.cursor() as cur:
            cur.execute("UPDATE users SET next_digest_at = now() + interval '20 days' "
                        "WHERE id = %s", (uid,))
            cur.execute("UPDATE user_clusters SET frequency = 'daily', send_weekday = NULL, "
                        "send_hour_local = NULL, timezone = NULL, next_digest_at = %s, "
                        "last_digest_at = NULL WHERE id = %s",
                        (slot_override, ids["a_search"]))
        work.commit()
        u = carica()
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET frequency = 'weekly', send_weekday = 5, "
                        "next_digest_at = %s WHERE id = %s",
                        (nuovo_override, ids["a_search"]))
        work.commit()
        worker._rischedula(work, u, datetime.now(timezone.utc), consegnato=True,
                           scheduled_for=slot_override,
                           ricerche_consegnate={ids["a_search"]})
        check("race override: nuova frequenza e next API conservati, last registrata",
              (seen_by_other("SELECT frequency || ':' || send_weekday FROM user_clusters "
                             "WHERE id = %s", (ids["a_search"],)),
               seen_by_other("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],))),
              ("weekly:5", nuovo_override, slot_override))

        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET frequency = 'daily', send_weekday = NULL, "
                        "send_hour_local = NULL, timezone = NULL, next_digest_at = %s, "
                        "last_digest_at = NULL WHERE id = %s",
                        (slot_override, ids["a_search"]))
        work.commit()
        u = carica()
        with work.cursor() as cur:
            cur.execute("UPDATE users SET send_hour_local = 22 WHERE id = %s", (uid,))
            cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id = %s",
                        (nuovo_override, ids["a_search"]))
        work.commit()
        worker._rischedula(work, u, datetime.now(timezone.utc), consegnato=True,
                           scheduled_for=slot_override,
                           ricerche_consegnate={ids["a_search"]})
        check("race sul fallback COALESCE: next nuovo preservato e last monotona",
              (seen_by_other("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],))),
              (nuovo_override, slot_override))

        slot_pausa_race = datetime.now(timezone.utc) - timedelta(minutes=10)
        with work.cursor() as cur:
            cur.execute("UPDATE users SET next_digest_at = %s, last_digest_at = NULL "
                        "WHERE id = %s", (slot_pausa_race, uid))
            cur.execute("UPDATE user_clusters SET is_paused = false, last_digest_at = NULL "
                        "WHERE id IN (%s, %s)", (ids["a_search"], ids["legacy_search"]))
            cur.execute("UPDATE user_clusters SET next_digest_at = %s WHERE id = %s",
                        (slot_pausa_race, ids["a_search"]))
            cur.execute("UPDATE user_clusters SET next_digest_at = now() + interval '20 days' "
                        "WHERE id = %s", (ids["b_search"],))
        work.commit()
        u = carica()
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = true WHERE id IN (%s, %s)",
                        (ids["a_search"], ids["legacy_search"]))
        work.commit()
        worker._rischedula(work, u, datetime.now(timezone.utc), consegnato=True,
                           scheduled_for=slot_pausa_race,
                           ricerche_consegnate={ids["a_search"], ids["legacy_search"]})
        check("pausa durante il worker: next non avanza ma la consegna resta nello storico",
              (seen_by_other("SELECT next_digest_at FROM users WHERE id = %s", (uid,)),
               seen_by_other("SELECT last_digest_at FROM users WHERE id = %s", (uid,)),
               seen_by_other("SELECT next_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],)),
               seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                             (ids["a_search"],))),
              (slot_pausa_race, slot_pausa_race, slot_pausa_race, slot_pausa_race))
        check("anche l'ereditata in pausa conserva last senza essere rischedulata",
              seen_by_other("SELECT last_digest_at FROM user_clusters WHERE id = %s",
                            (ids["legacy_search"],)), slot_pausa_race)
    finally:
        worker._rischedula = originale_rischedula
        worker.email_mod.invia = originale_invia
        worker.email_mod.anteprima = originale_anteprima
        worker.prescore_mod.disponibile = originale_prescore
        worker.push_mod.invia_digest_pronto = originale_push


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
        check_modelli_e_analisi()
        wipe(work)
        ids = seed(work)

        with work.cursor() as cur:
            cur.execute(
                "INSERT INTO jobs (source, source_job_id, url, canonical_url, "
                "  title, title_normalized, date_posted, raw, link_kind, "
                "  salary_min, salary_currency, salary_period, salary_da) "
                "VALUES ('fantastic', 'low-salary', 'https://acme.example/low', "
                "  'https://acme.example/low', 'Low salary', 'Low salary', now(), "
                "  '{}'::jsonb, 'career_site', 40000, 'EUR', 'year', 'dichiarato') "
                "RETURNING id")
            low_id = cur.fetchone()[0]
            cur.execute("INSERT INTO job_clusters (job_id, cluster_id) VALUES (%s, %s)",
                        (low_id, ids["cluster"]))
            cur.execute(
                "UPDATE user_clusters SET min_salary_annual = 50000, "
                "  salary_currency = 'EUR', desired_technologies = ARRAY['Python']")
        work.commit()
        candidates = candidati(work, ids["user"])
        ids_candidati = {row["source_job_id"] for row in candidates}
        check("salario dichiarato sotto il minimo viene escluso",
              "low-salary" not in ids_candidati, True)
        check("offerta senza salario resta valutabile",
              "live" in ids_candidati, True)
        check("tecnologie desiderate viaggiano solo come preferenza",
              all("Python" in (row.get("_wants") or "") for row in candidates), True)
        work.rollback()

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
              seen_by_other("SELECT count(*) FROM jobs"), 4)

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
        check("la retention tecnica non cancella feedback o tracker personale",
              seen_by_other("SELECT saved_at IS NOT NULL AND application_status = 'applied' "
                            "AND applied_at IS NOT NULL AND action_version = 1 "
                            "FROM matches WHERE id = %s", (ids["match"],)), True)

        section("GDPR end-to-end")
        req = request_deletion(work, ids["user"])
        check("COMMITTATO: utente marcato deleted, visibile da fuori",
              seen_by_other("SELECT status = 'deleted' AND deleted_at IS NOT NULL "
                            "AND next_digest_at IS NULL FROM users WHERE id = %s",
                            (ids["user"],)), True)
        check("richiesta registrata con le chiavi di CV e documenti da rimuovere",
              seen_by_other("SELECT jsonb_array_length(pending_storage_keys) "
                            "FROM deletion_requests WHERE id = %s", (req,)), 2)
        check("seconda richiesta: ON CONFLICT tiene la prima",
              request_deletion(work, ids["user"]), req)
        check("una sola richiesta aperta",
              seen_by_other("SELECT count(*) FROM deletion_requests WHERE user_id = %s",
                            (ids["user"],)), 1)

        totals = execute_deletion(work, req, batch_size=100)
        check("cancellati i dati personali attesi",
              sorted(totals), ["api_usage:anonimizzate", "digest_items", "digests",
                               "login_tokens", "match_documents", "match_interviews",
                               "matches", "oauth_identities", "personal_notification_dedup",
                               "sessions", "user_clusters", "user_cvs", "users"])
        for table in ("user_cvs", "user_clusters", "matches", "digest_items", "digests",
                      "login_tokens", "sessions", "oauth_identities", "match_documents",
                      "match_interviews", "personal_notification_dedup"):
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
        adesso = datetime.now(timezone.utc)

        from psycopg.rows import dict_row
        with work.cursor(row_factory=dict_row) as cur:
            dovuti = worker.utenti_dovuti(cur, adesso)
        per_email = {u.email: u for u in dovuti}
        check("due utenti dovuti trovati", sorted(per_email), [
            "digest-a@example.test", "digest-b@example.test"])

        def digest_normale(utente, valutatore):
            originale = worker.email_mod.invia
            worker.email_mod.invia = lambda *a, **kw: "<msgid@test>"
            try:
                return worker.digest_utente(work, utente, dry_run=False,
                                            evaluatore=valutatore)
            finally:
                worker.email_mod.invia = originale

        slot_iniziale = seen_by_other(
            "SELECT next_digest_at FROM users WHERE id = %s", (dg["a"],))
        coppie_prescore = []
        originale_disponibile = worker.prescore_mod.disponibile
        originale_prescore = worker.prescore_mod.prescore_batch
        originale_anteprima = worker.email_mod.anteprima

        def prescore_finto(profilo, testi):
            coppie_prescore.extend((profilo, testo) for testo in testi)
            return [100.0] * len(testi)

        worker.prescore_mod.disponibile = lambda: True
        worker.prescore_mod.prescore_batch = prescore_finto
        worker.email_mod.anteprima = lambda *a, **kw: "<anteprima-finta>"
        try:
            e_dry = worker.digest_utente(
                work, per_email["digest-a@example.test"], dry_run=True,
                evaluatore=ValutatoreFinto(
                    {"ok1": 90, "ok2": 85, "nulli": 85, "bassa": 40}))
        finally:
            worker.prescore_mod.disponibile = originale_disponibile
            worker.prescore_mod.prescore_batch = originale_prescore
            worker.email_mod.anteprima = originale_anteprima
        check("dry-run compila ma non dichiara un invio", e_dry["stato"], "dry_run")
        check("dry-run valuta gli stessi 4 candidati e prepara 3 voci",
              (e_dry["valutate"], e_dry["inviate"]), (4, 3))
        check("dry-run non lascia alcun digest",
              seen_by_other("SELECT count(*) FROM digests WHERE user_id = %s",
                            (dg["a"],)), 0)
        check("dry-run non lascia match né anti-ripetizione",
              seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                            (dg["a"],)), 0)
        check("dry-run non consuma il budget",
              seen_by_other("SELECT count(*) FROM user_evaluation_budget "
                            "WHERE user_id = %s", (dg["a"],)), 0)
        check("dry-run non brucia lo slot",
              seen_by_other("SELECT next_digest_at FROM users WHERE id = %s",
                            (dg["a"],)), slot_iniziale)
        check("dry-run non scrive usage",
              seen_by_other("SELECT count(*) FROM api_usage WHERE user_id = %s",
                            (dg["a"],)), 0)
        check("prescore usa il formatter condiviso con `_wants`",
              len(coppie_prescore) == 4 and all(
                  "Voglio crescere nel recruiting" in testo
                  for _, testo in coppie_prescore), True)

        chiamate_famiglia = []
        originale_famiglia = worker.prescore_mod.famiglia_batch

        def famiglia_finta(offerte):
            chiamate_famiglia.append(len(offerte))
            return [{"Human Resources": 0.05, "Security & Safety": 0.95}
                    if "bassa" in o["title"].lower() else {"Human Resources": 1.0}
                    for o in offerte]

        def famiglia_rotta(offerte):
            raise RuntimeError("family guard finto irraggiungibile")

        worker.prescore_mod.disponibile = lambda: True
        worker.prescore_mod.prescore_batch = prescore_finto
        worker.email_mod.anteprima = lambda *a, **kw: "<anteprima-finta>"
        try:
            with mock.patch.dict(os.environ, {"FAMILY_GUARD": "1"}):
                worker.prescore_mod.famiglia_batch = famiglia_finta
                e_guard = worker.digest_utente(
                    work, per_email["digest-a@example.test"], dry_run=True,
                    evaluatore=ValutatoreFinto(
                        {"ok1": 90, "ok2": 85, "nulli": 85, "bassa": 40}))
                worker.prescore_mod.famiglia_batch = famiglia_rotta
                e_guard_rotto = worker.digest_utente(
                    work, per_email["digest-a@example.test"], dry_run=True,
                    evaluatore=ValutatoreFinto(
                        {"ok1": 90, "ok2": 85, "nulli": 85, "bassa": 40}))
        finally:
            worker.prescore_mod.disponibile = originale_disponibile
            worker.prescore_mod.prescore_batch = originale_prescore
            worker.prescore_mod.famiglia_batch = originale_famiglia
            worker.email_mod.anteprima = originale_anteprima
        check("family guard esclude Security prima del valutatore",
              (e_guard["valutate"], e_guard["inviate"], chiamate_famiglia), (3, 3, [4]))
        check("family guard irraggiungibile conserva tutti i candidati",
              (e_guard_rotto["valutate"], e_guard_rotto["inviate"]), (4, 3))

        finto = ValutatoreFinto({"ok1": 90, "ok2": 85, "nulli": 85, "bassa": 40})
        slot_atteso = worker.prossimo_slot(per_email["digest-a@example.test"],
                                           datetime.now(timezone.utc))
        e = digest_normale(per_email["digest-a@example.test"], finto)
        check("digest A inviato", e["stato"], "sent")
        check("digest A: 4 valutate, 3 inviate", (e["valutate"], e["inviate"]), (4, 3))
        check("COMMITTATO: digest sent visibile da fuori",
              seen_by_other("SELECT status FROM digests WHERE user_id = %s",
                            (dg["a"],)), "sent")
        check("COMMITTATO: 4 match scritti, scarti compresi",
              seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                            (dg["a"],)), 4)
        check("la valutazione consuma 4 quote, non le motivazioni",
              seen_by_other("SELECT evaluations_used FROM user_evaluation_budget "
                            "WHERE user_id = %s", (dg["a"],)), 4)
        check("l'offerta sotto soglia è registrata come non passata",
              seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s AND NOT passed",
                            (dg["a"],)), 1)
        check("i filtri deterministici hanno escluso 3 offerte SENZA valutarle",
              seen_by_other("SELECT count(*) FROM matches m JOIN jobs j ON j.id = m.job_id "
                            "WHERE m.user_id = %s AND j.source_job_id IN "
                            "('francese','junior','junior senza tag')",
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
        check("la seconda passata recupera desiderio e blocchi completi",
              all("Voglio crescere" in (item.get("_wants") or "")
                  and item.get("ai_core_responsibilities") == "responsabilità finte"
                  and item.get("ai_requirements_summary") == "requisiti finti"
                  and item.get("ai_benefits") == "benefit finti"
                  for item in finto.motivazioni_viste[-3:]), True)
        check("rischedulato all'orario locale, incluso il cambio DST",
              seen_by_other("SELECT next_digest_at FROM users WHERE id = %s",
                            (dg["a"],)), slot_atteso)

        di_nuovo = worker.digest_utente(work, per_email["digest-a@example.test"],
                                        dry_run=True, evaluatore=finto)
        check("lo slot già consegnato non si ripete", di_nuovo["stato"], "già consegnato")

        with work.cursor() as cur:
            cur.execute("UPDATE users SET next_digest_at = now() - interval '30 min' "
                        "WHERE id = %s", (dg["a"],))
        work.commit()
        with work.cursor(row_factory=dict_row) as cur:
            u2 = next(u for u in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                      if u.email == "digest-a@example.test")
        e2 = digest_normale(u2, finto)
        check("senza offerte nuove il digest è skipped_empty", e2["stato"], "skipped_empty")
        check("l'anti-ripetizione non ha rivalutato nulla",
              seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                            (dg["a"],)), 4)
        check("un digest vuoto è un esito legittimo, distinto dal fallimento",
              seen_by_other("SELECT count(*) FROM digests WHERE user_id = %s "
                            "  AND status = 'skipped_empty'", (dg["a"],)), 1)
        with work.cursor() as cur:
            cur.execute("UPDATE matches SET rubric_version = 'precedente' WHERE user_id = %s",
                        (dg["a"],))
        work.commit()
        check("i match con vecchia rubrica rientrano nella valutazione",
              len(funnel_mod.candidati(work, dg["a"])), 4)
        with work.cursor() as cur:
            cur.execute("UPDATE matches SET rubric_version = %s WHERE user_id = %s",
                        (llm_mod.VERSIONE_RUBRICA, dg["a"]))
        work.commit()
        check("i match già aggiornati non vengono ripagati",
              funnel_mod.candidati(work, dg["a"]), [])

        eb = worker.digest_utente(work, per_email["digest-b@example.test"],
                                  dry_run=False, evaluatore=finto)
        check("budget esaurito: digest fallito con motivo esplicito",
              eb["stato"], "failed_budget")
        check("il messaggio dice piano e consumato",
              seen_by_other("SELECT error_message FROM digests WHERE user_id = %s "
                            "  AND status = 'failed'", (dg["b"],)),
              "budget di valutazione esaurito (4000/4000, piano pro)")

        with work.cursor() as cur:
            cur.execute(
                "INSERT INTO matches (user_id, job_id, score, reason, threshold_used, model, "
                "rubric_version) SELECT %s, id, 88, 'rimasta indietro', 95, 'test', %s "
                "FROM jobs WHERE source_job_id = 'francese' RETURNING id",
                (dg["a"], llm_mod.VERSIONE_RUBRICA))
            match_recupero = cur.fetchone()[0]
            cur.execute("UPDATE users SET next_digest_at = now() - interval '20 min' "
                        "WHERE id = %s", (dg["a"],))
            cur.execute("UPDATE user_clusters SET min_score = 90 "
                        "WHERE user_id = %s AND cluster_id = %s",
                        (dg["a"], dg["cluster"]))
        work.commit()
        predicato = funnel_mod.match_attivo_sql()
        check("match passed=false storico escluso dalla soglia corrente 90",
              seen_by_other("SELECT count(*) FROM matches m WHERE m.id = %s AND "
                            + predicato, (match_recupero,)), 0)
        check("digest vuoto se la soglia corrente è più alta dello score",
              len(worker._items_del_digest(work, dg["a"])), 0)
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET min_score = 80 "
                        "WHERE user_id = %s AND cluster_id = %s",
                        (dg["a"], dg["cluster"]))
        work.commit()
        recuperi = worker._items_del_digest(work, dg["a"])
        check("abbassare la soglia recupera il match senza ripagare il modello",
              [(item["match_id"], item["score"]) for item in recuperi],
              [(str(match_recupero), 88)])
        check("il recupero ha `_wants` e annuncio completo",
              bool(recuperi) and "Voglio crescere" in recuperi[0]["_wants"]
              and recuperi[0]["ai_working_hours"] == "orario finto", True)
        with work.cursor() as cur:
            cur.execute("INSERT INTO clusters (family, country) "
                        "VALUES ('Human Resources', 'DE') RETURNING id")
            secondo_cluster = cur.fetchone()[0]
            cur.execute("INSERT INTO job_clusters (job_id, cluster_id) "
                        "SELECT job_id, %s FROM matches WHERE id = %s",
                        (secondo_cluster, match_recupero))
            cur.execute("INSERT INTO user_clusters (user_id, cluster_id, min_score) "
                        "VALUES (%s, %s, 95)", (dg["a"], secondo_cluster))
        work.commit()
        check("una seconda ricerca più restrittiva non alza il minimo",
              seen_by_other("SELECT count(*) FROM matches m WHERE m.id = %s AND "
                            + predicato, (match_recupero,)), 1)
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = true "
                        "WHERE user_id = %s AND cluster_id = %s",
                        (dg["a"], dg["cluster"]))
        work.commit()
        check("pausa della ricerca che rende eleggibile il match lo nasconde",
              seen_by_other("SELECT count(*) FROM matches m WHERE m.id = %s AND "
                            + predicato, (match_recupero,)), 0)
        check("lo storico già consegnato non viene cancellato dai filtri correnti",
              seen_by_other("SELECT count(*) FROM digest_items WHERE user_id = %s",
                            (dg["a"],)), 3)
        with work.cursor() as cur:
            cur.execute("UPDATE user_clusters SET is_paused = false "
                        "WHERE user_id = %s AND cluster_id = %s",
                        (dg["a"], dg["cluster"]))
        work.commit()

        section("azioni personali, digest e date oneste")
        dichiarato = dict(recuperi[0], date_posted_declared=True)
        _, testo_dichiarato, _ = worker.email_mod.compila([dichiarato], "it")
        testi_dichiarati = [testo_dichiarato,
                            *worker.telegram_mod.compila([dichiarato], "it"),
                            *worker.whatsapp_mod._compila([dichiarato], "it", None)]
        check("i tre delivery mostrano 'pubblicata' solo con prova della fonte",
              all("pubblicata il" in testo for testo in testi_dichiarati), True)
        for provenienza in (False, None):
            stimato = dict(recuperi[0], date_posted_declared=provenienza)
            _, testo_stimato, _ = worker.email_mod.compila([stimato], "it")
            testi_stimati = [testo_stimato,
                             *worker.telegram_mod.compila([stimato], "it"),
                             *worker.whatsapp_mod._compila([stimato], "it", None)]
            check(f"nessuna etichetta 'pubblicata' con provenienza {provenienza!r}",
                  any("pubblicata il" in testo for testo in testi_stimati), False)

        def cambia_azione(sql):
            with work.cursor() as cur:
                cur.execute(sql, (match_recupero,))
            work.commit()

        cambia_azione(
            "UPDATE matches SET saved_at = NULL, dismissed_at = now(), "
            "dismiss_reason = 'target', archived_at = NULL, "
            "application_status = 'not_started', applied_at = NULL, "
            "action_version = 1, user_action_at = now() WHERE id = %s")
        check("uno scartato non rientra in un digest nuovo",
              worker._items_del_digest(work, dg["a"]), [])
        cambia_azione(
            "UPDATE matches SET dismissed_at = NULL, dismiss_reason = NULL, "
            "archived_at = now(), action_version = 2, user_action_at = now() "
            "WHERE id = %s")
        check("un archiviato non rientra in un digest nuovo",
              worker._items_del_digest(work, dg["a"]), [])
        cambia_azione(
            "UPDATE matches SET archived_at = NULL, application_status = 'applied', "
            "applied_at = now(), action_version = 3, user_action_at = now() "
            "WHERE id = %s")
        check("una candidatura avviata non rientra in un digest nuovo",
              worker._items_del_digest(work, dg["a"]), [])
        cambia_azione(
            "UPDATE matches SET saved_at = now() - interval '2 days', "
            "application_status = 'not_started', applied_at = NULL, "
            "action_version = 4, user_action_at = now() WHERE id = %s")
        check("il salvataggio non viene confuso con scarto o archivio",
              [item["match_id"] for item in worker._items_del_digest(work, dg["a"])],
              [str(match_recupero)])

        with work.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email,plan,subscription_status,delivery_channels,"
                "frequency,timezone) VALUES ('cv-reopen@example.test','pro','active',"
                "'{email}','daily','UTC') RETURNING id")
            cv_uid = cur.fetchone()[0]
            for sid, salvato in (("saved", True), ("pristine", False)):
                cur.execute(
                    "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,"
                    "title_normalized,organization,date_posted,raw,link_kind) VALUES "
                    "('fantastic',%s,%s,%s,'CV reopen','cv reopen','Acme',now(),"
                    "'{}'::jsonb,'career_site') RETURNING id",
                    (f"cv-reopen-{sid}", f"https://acme.example/cv-reopen-{sid}",
                     f"https://acme.example/cv-reopen-{sid}"))
                cv_job = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO matches (user_id,job_id,score,reason,threshold_used,model,"
                    "saved_at,action_version,user_action_at) VALUES "
                    "(%s,%s,88,'cv',70,'test',CASE WHEN %s THEN now() ELSE NULL END,"
                    "%s,CASE WHEN %s THEN now() ELSE NULL END) RETURNING id",
                    (cv_uid, cv_job, salvato, 1 if salvato else 0, salvato))
                if salvato:
                    cv_match_salvato = cur.fetchone()[0]
        work.commit()
        with work.cursor() as cur:
            riaperti = funnel_mod.elimina_match_da_rivalutare(cur, cv_uid)
        work.commit()
        check("nuovo CV/profilo: solo il match vergine viene riaperto", riaperti, 1)
        check("nuovo CV/profilo: salvataggio e versione non spariscono",
              seen_by_other("SELECT saved_at IS NOT NULL AND action_version = 1 "
                            "FROM matches WHERE id = %s", (cv_match_salvato,)), True)
        check("nuovo CV/profilo: il match senza azioni e' eliminato",
              seen_by_other("SELECT count(*) FROM matches m JOIN jobs j ON j.id = m.job_id "
                            "WHERE j.source_job_id = 'cv-reopen-pristine'"), 0)

        with work.cursor(row_factory=dict_row) as cur:
            u3 = next(u for u in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                      if u.email == "digest-a@example.test")
        e3 = digest_normale(u3, finto)
        check("il match rimasto indietro viene consegnato", e3["stato"], "sent")
        check("senza rivalutarlo: zero chiamate nuove",
              (e3["valutate"], e3["inviate"]), (0, 1))
        check("il conteggio del digest conta le valutazioni che lo alimentano",
              seen_by_other("SELECT jobs_evaluated_count || '/' || jobs_sent_count "
                            "FROM digests WHERE user_id = %s AND status = 'sent' "
                            "  AND jobs_sent_count = 1", (dg["a"],)), "1/1")

        section("cap di 30 nuove valutazioni per digest")

        def seed_cap():
            cap_dg = seed_digest(work)
            with work.cursor() as cur:
                for i in range(35):
                    sid = f"cap-{i:02d}"
                    cur.execute(
                        "INSERT INTO jobs (source,source_job_id,url,canonical_url,title,"
                        "title_normalized,organization,date_posted,countries,ai_job_language,"
                        "ai_experience_level,ai_employment_type,ai_key_skills,"
                        "ai_requirements_summary,ai_core_responsibilities,ai_working_hours,"
                        "raw,link_kind,org_headcount) VALUES "
                        "('fantastic',%s,%s,%s,%s,%s,'Acme SpA',now(),ARRAY['IT'],"
                        "'Italian','5-10','FULL_TIME',ARRAY['recruiting'],"
                        "'requisiti finti','responsabilità finte','orario finto',"
                        "jsonb_build_object('ai_benefits','benefit finti'),"
                        "'career_site',500) RETURNING id",
                        (sid, f"https://acme.example/{sid}", f"https://acme.example/{sid}",
                         f"Offerta {sid}", f"offerta {sid}"))
                    cap_job = cur.fetchone()[0]
                    cur.execute("INSERT INTO job_clusters (job_id,cluster_id) VALUES (%s,%s)",
                                (cap_job, cap_dg["cluster"]))
            work.commit()
            return cap_dg

        def utente_cap():
            with work.cursor(row_factory=dict_row) as cur:
                return next(u for u in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                            if u.email == "digest-a@example.test")

        def digest_cap(modalita):
            originale_disponibile = worker.prescore_mod.disponibile
            originale_batch = worker.prescore_mod.prescore_batch
            if modalita == "attivo":
                worker.prescore_mod.disponibile = lambda: True
                worker.prescore_mod.prescore_batch = lambda profilo, testi: [100.0] * len(testi)
            elif modalita == "spento":
                worker.prescore_mod.disponibile = lambda: False
            else:
                worker.prescore_mod.disponibile = lambda: True
                def fallisce(*_args):
                    raise RuntimeError("pre-scorer finto irraggiungibile")
                worker.prescore_mod.prescore_batch = fallisce
            try:
                return digest_normale(utente_cap(), ValutatoreFinto(
                    {f"cap-{i:02d}": 90 for i in range(35)}))
            finally:
                worker.prescore_mod.disponibile = originale_disponibile
                worker.prescore_mod.prescore_batch = originale_batch

        for modalita in ("attivo", "spento", "errore"):
            wipe(work)
            cap_dg = seed_cap()
            esito_cap = digest_cap(modalita)
            check(f"cap 30 con pre-scorer {modalita}: nuove valutazioni e consegne",
                  (esito_cap["valutate"], esito_cap["inviate"]), (30, 30))
            check(f"cap 30 con pre-scorer {modalita}: quota e match scritti",
                  (seen_by_other("SELECT evaluations_used FROM user_evaluation_budget "
                                 "WHERE user_id = %s", (cap_dg["a"],)),
                   seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                                 (cap_dg["a"],))), (30, 30))
            check(f"cap 30 con pre-scorer {modalita}: conteggio senza recuperi",
                  seen_by_other("SELECT jobs_evaluated_count FROM digests "
                                "WHERE user_id = %s AND status = 'sent'", (cap_dg["a"],)), 30)

        wipe(work)
        cap_dg = seed_cap()
        with work.cursor() as cur:
            cur.execute(
                "INSERT INTO user_evaluation_budget (user_id,period_month,evaluations_used) "
                "VALUES (%s,date_trunc('month',current_date)::date,3990)", (cap_dg["a"],))
        work.commit()
        esito_cap = digest_cap("spento")
        check("il budget residuo sotto 30 prevale sul cap",
              (esito_cap["valutate"],
               seen_by_other("SELECT evaluations_used FROM user_evaluation_budget "
                             "WHERE user_id = %s", (cap_dg["a"],)),
               seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                             (cap_dg["a"],))), (10, 4000, 10))

        wipe(work)
        cap_dg = seed_cap()
        check("primo giro limitato a 30 anche senza pre-scorer",
              digest_cap("spento")["valutate"], 30)
        with work.cursor() as cur:
            cur.execute("UPDATE users SET next_digest_at = now() - interval '1 min' "
                        "WHERE id = %s", (cap_dg["a"],))
        work.commit()
        esito_cap = digest_cap("spento")
        check("le offerte escluse dal cap tornano nel giro successivo",
              (esito_cap["valutate"],
               seen_by_other("SELECT evaluations_used FROM user_evaluation_budget "
                             "WHERE user_id = %s", (cap_dg["a"],)),
               seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                             (cap_dg["a"],))), (9, 39, 39))

        wipe(work)
        dg = seed_digest(work)

        with work.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, plan, subscription_status, delivery_channels, "
                "  frequency, timezone, next_digest_at) VALUES "
                "('digest-c@example.test','pro','active','{email}','daily','Europe/Rome', "
                " now() - interval '15 min') RETURNING id")
            uc_id = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO user_cvs (user_id, storage_key, families, seniority, "
                "  skills, encryption_algo, encrypted_dek, nonce, auth_tag, kek_version) "
                "VALUES (%s, 'cv/digest-c.pdf', ARRAY['Human Resources'], '2-5', "
                "  ARRAY['payroll'], 'aes-256-gcm', decode(repeat('ab',32),'hex'), "
                "  decode(repeat('cd',12),'hex'), decode(repeat('ef',16),'hex'), 1)",
                (uc_id,))
            cur.execute("INSERT INTO user_clusters (user_id, cluster_id, languages, "
                        "  min_seniority, max_seniority, employment_types) VALUES "
                        "(%s,%s, ARRAY['Italian'], '2-5', '10+', ARRAY['FULL_TIME'])",
                        (uc_id, dg["cluster"]))
        work.commit()

        invia_originale = worker.email_mod.invia

        def invia_che_falla(*a, **kw):
            raise RuntimeError("SMTP finto: connessione rifiutata")

        worker.email_mod.invia = invia_che_falla
        try:
            with work.cursor(row_factory=dict_row) as cur:
                uc = next(u for u in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                          if u.email == "digest-c@example.test")
            try:
                worker.digest_utente(work, uc, dry_run=False, evaluatore=finto)
                bad("l'invio fallito deve sollevare", "non ha sollevato")
            except RuntimeError:
                ok("l'invio fallito solleva", "RuntimeError")
            check("nessuna voce di digest registrata dopo il fallimento",
                  seen_by_other("SELECT count(*) FROM digest_items di "
                                "JOIN users u ON u.id = di.user_id "
                                "WHERE u.email = 'digest-c@example.test'"), 0)
            check("il digest resta da riprendere, non risulta consegnato",
                  seen_by_other("SELECT d.status FROM digests d "
                                "JOIN users u ON u.id = d.user_id "
                                "WHERE u.email = 'digest-c@example.test'"), "pending")
            check("il guasto email ha un contatore proprio",
                  seen_by_other("SELECT delivery_failures_by_channel->>'email' "
                                "FROM users WHERE id = %s", (uc_id,)), "1")

            with work.cursor() as cur:
                cur.execute("UPDATE users SET delivery_failures_by_channel = "
                            "'{\"email\": 1, \"telegram\": 2}'::jsonb WHERE id = %s",
                            (uc_id,))
            work.commit()
            worker.email_mod.invia = lambda *a, **kw: "<msgid@test>"
            with work.cursor(row_factory=dict_row) as cur:
                uc2 = next(u for u in worker.utenti_dovuti(cur, datetime.now(timezone.utc))
                           if u.email == "digest-c@example.test")
            e4 = worker.digest_utente(work, uc2, dry_run=False, evaluatore=finto)
            check("al retry il digest parte senza rivalutare",
                  (e4["stato"], e4["valutate"]), ("sent", 0))
            check("il recovery non addebita due volte le valutazioni già committate",
                  seen_by_other("SELECT evaluations_used FROM user_evaluation_budget "
                                "WHERE user_id = %s", (uc_id,)), 4)
            check("le offerte sono nel digest del retry",
                  seen_by_other("SELECT count(*) FROM digest_items di "
                                "JOIN users u ON u.id = di.user_id "
                                "WHERE u.email = 'digest-c@example.test'"), 3)
            check("il successo email azzera solo email, non gli altri canali",
                  seen_by_other("SELECT (delivery_failures_by_channel->>'email') || ':' || "
                                "(delivery_failures_by_channel->>'telegram') "
                                "FROM users WHERE id = %s", (uc_id,)), "0:2")
        finally:
            worker.email_mod.invia = invia_originale

        check_frequenza_per_ricerca(work, seen_by_other)
        check_recupero_e_race(work, seen_by_other)

        section("usage reale, risposte invalide e push (solo mock)")
        wipe(work)
        with work.cursor() as cur:
            cur.execute("INSERT INTO api_usage (provider, operation, model, requests) "
                        "VALUES ('groq', 'score', 'costo-sconosciuto', 1) "
                        "RETURNING cost_micros")
            costo_omesso = cur.fetchone()[0]
            cur.execute("DELETE FROM api_usage WHERE model = 'costo-sconosciuto'")
        work.commit()
        check("il default del ledger per un costo ignoto è NULL",
              costo_omesso, None)
        with work.cursor() as cur:
            cur.execute("INSERT INTO clusters (family, country) "
                        "VALUES ('Human Resources', 'IT') RETURNING id")
            cluster_uso = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO jobs (source, source_job_id, url, canonical_url, "
                "  domain_derived, title, title_normalized, organization, date_posted, "
                "  countries, ai_job_language, ai_experience_level, ai_key_skills, "
                "  ai_requirements_summary, ai_core_responsibilities, "
                "  ai_working_hours, raw, link_kind) VALUES "
                "('fantastic', 'usage-job', 'https://acme.example/usage', "
                " 'https://acme.example/usage', 'acme.example', 'HR Manager', "
                " 'hr manager', 'Acme', now(), ARRAY['IT'], 'Italian', '5-10', "
                " ARRAY['recruiting'], 'requisiti veri', 'responsabilità vere', "
                " 'orario vero', jsonb_build_object('ai_benefits', 'benefit veri'), "
                " 'career_site') RETURNING id")
            job_uso = cur.fetchone()[0]
            cur.execute("INSERT INTO job_clusters (job_id, cluster_id) VALUES (%s, %s)",
                        (job_uso, cluster_uso))

            def utente_uso(email, canali):
                cur.execute(
                    "INSERT INTO users (email, plan, subscription_status, "
                    "  delivery_channels, frequency, timezone, next_digest_at) "
                    "VALUES (%s, 'pro', 'active', %s, 'daily', 'Europe/Rome', "
                    " now() - interval '10 min') RETURNING id", (email, canali))
                uid = cur.fetchone()[0]
                cur.execute(
                    "INSERT INTO user_cvs (user_id, storage_key, families, seniority, "
                    "  skills, languages, years_experience, encryption_algo, encrypted_dek, "
                    "  nonce, auth_tag, kek_version) VALUES "
                    "(%s, 'cv/usage.pdf', ARRAY['Human Resources'], '5-10', "
                    " ARRAY['recruiting'], '[\"Italian\"]'::jsonb, 8, 'aes-256-gcm', "
                    " decode(repeat('ab',32),'hex'), decode(repeat('cd',12),'hex'), "
                    " decode(repeat('ef',16),'hex'), 1)", (uid,))
                cur.execute("INSERT INTO user_clusters (user_id, cluster_id, wants) "
                            "VALUES (%s, %s, 'Voglio il recruiting')", (uid, cluster_uso))
                return uid

            uid_uso = utente_uso("usage-app@example.test", ["app"])
            uid_invalido = utente_uso("usage-invalid@example.test", ["email"])
            cur.execute("INSERT INTO model_pricing (model, input_per_mtok, "
                        "  cached_per_mtok, output_per_mtok) "
                        "VALUES ('locale-reale-finto', 1.0, 0.5, 2.0) "
                        "ON CONFLICT (model) DO UPDATE SET input_per_mtok = 1.0, "
                        " cached_per_mtok = 0.5, output_per_mtok = 2.0")
        work.commit()

        motivazione_ok = {
            "reason": "Recruiting allineato", "prova": [1],
            "perche": "Hai recruiting", "pros": ["Recruiting"],
            "cons": ["Da verificare"], "responsabilita": "responsabilità vere",
            "requisiti": "requisiti veri", "benefit": "benefit veri"}

        def evaluatori_uso():
            primo = ModelloFinto("groq", "groq-malformato-db", [{"reason": "manca"}])
            secondo = ModelloFinto("maestro-locale", "locale-reale-finto", [
                {"score": 90, "reason": "allineato"}, motivazione_ok,
                {"inventato": False}])
            return valutatore_da([primo, secondo])

        with work.cursor(row_factory=dict_row) as cur:
            utenti_uso = {u.email: u for u in worker.utenti_dovuti(
                cur, datetime.now(timezone.utc))}
        anteprima_originale = worker.email_mod.anteprima
        worker.email_mod.anteprima = lambda *a, **kw: "<dry-run>"
        try:
            esito_dry_uso = worker.digest_utente(
                work, utenti_uso["usage-app@example.test"], dry_run=True,
                evaluatore=evaluatori_uso())
        finally:
            worker.email_mod.anteprima = anteprima_originale
        check("dry-run con valutatore reale non scrive nemmeno il ledger",
              (esito_dry_uso["stato"],
               seen_by_other("SELECT count(*) FROM api_usage WHERE user_id = %s",
                             (uid_uso,)),
               seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                             (uid_uso,))), ("dry_run", 0, 0))

        def push_che_falla(*a, **kw):
            raise RuntimeError("Expo finto irraggiungibile")

        push_originale_worker = worker.push_mod.invia_digest_pronto
        worker.push_mod.invia_digest_pronto = push_che_falla
        try:
            esito_uso = worker.digest_utente(
                work, utenti_uso["usage-app@example.test"], dry_run=False,
                evaluatore=evaluatori_uso())
        finally:
            worker.push_mod.invia_digest_pronto = push_originale_worker
        check("un'eccezione push non invalida il digest appena consegnato",
              (esito_uso["stato"],
               seen_by_other("SELECT status FROM digests WHERE user_id = %s",
                             (uid_uso,))), ("sent", "sent"))
        check("fallback: solo una quota consumata, niente quota per la risposta rotta",
              seen_by_other("SELECT evaluations_used FROM user_evaluation_budget "
                            "WHERE user_id = %s", (uid_uso,)), 1)
        check("match e seconda passata registrano il modello/provider davvero usati",
              seen_by_other("SELECT model || ':' || provider || ':' || analysis_model "
                            "|| ':' || analysis_provider FROM matches WHERE user_id = %s",
                            (uid_uso,)),
              "locale-reale-finto:maestro-locale:locale-reale-finto:maestro-locale")
        check("seconda passata aggrega generazione e verificatore",
              seen_by_other("SELECT analysis_input_tokens || '/' || analysis_output_tokens "
                            "FROM matches WHERE user_id = %s", (uid_uso,)), "200/20")
        check("usage distingue score e motivation per modello",
              seen_by_other("SELECT string_agg(operation || ':' || model || ':' || requests, "
                            " ',' ORDER BY operation) FROM api_usage WHERE user_id = %s",
                            (uid_uso,)),
              "motivation:locale-reale-finto:2,score:groq-malformato-db:1,"
              "score:locale-reale-finto:1")
        check("costo ignoto del modello malformato è NULL, non zero",
              seen_by_other("SELECT cost_micros IS NULL FROM api_usage "
                            "WHERE user_id = %s AND model = 'groq-malformato-db'",
                            (uid_uso,)), True)
        check("costo noto usa il pricing del modello davvero usato",
              seen_by_other("SELECT bool_and(cost_micros > 0) FROM api_usage "
                            "WHERE user_id = %s AND model = 'locale-reale-finto'",
                            (uid_uso,)), True)

        valutatore_invalido = valutatore_da([
            ModelloFinto("groq", "groq-malformato-db", [{"score": None}])])
        try:
            worker.digest_utente(work, utenti_uso["usage-invalid@example.test"],
                                 dry_run=False, evaluatore=valutatore_invalido)
        except RuntimeError as exc:
            worker._segna_fallito(work, utenti_uso["usage-invalid@example.test"],
                                  str(exc))
            ok("score invalido senza fallback arriva all'errore del digest", str(exc)[:80])
        else:
            bad("score invalido senza fallback arriva all'errore del digest",
                "digest accettato")
        check("risposta invalida: nessun match definitivo e nessun budget bruciato",
              (seen_by_other("SELECT count(*) FROM matches WHERE user_id = %s",
                             (uid_invalido,)),
               seen_by_other("SELECT count(*) FROM user_evaluation_budget "
                             "WHERE user_id = %s", (uid_invalido,)),
               seen_by_other("SELECT status FROM digests WHERE user_id = %s",
                             (uid_invalido,))), (0, 0, "failed"))
        check("usage della risposta invalida conserva costo NULL",
              seen_by_other("SELECT cost_micros IS NULL FROM api_usage "
                            "WHERE user_id = %s", (uid_invalido,)), True)

        token_validi = [f"ExponentPushToken[t{i:04d}]" for i in range(205)]
        morti_attesi = {token_validi[100], token_validi[204]}
        with work.cursor() as cur:
            cur.executemany(
                "INSERT INTO user_push_tokens (user_id, token, platform) "
                "VALUES (%s, %s, 'ios')",
                [(uid_uso, token) for token in token_validi + ["token-non-expo"]])
        work.commit()
        payload_expo = []

        class RispostaExpo:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [
                    {"status": "error",
                     "details": {"error": "DeviceNotRegistered"}}
                    if messaggio["to"] in morti_attesi else {"status": "ok"}
                    for messaggio in self.payload]}

        def post_expo(url, *, json, timeout, headers):
            payload_expo.append(json)
            return RispostaExpo(json)

        post_originale = push_mod.httpx.post
        push_mod.httpx.post = post_expo
        try:
            notificati = push_mod.invia_digest_pronto(work, uid_uso)
        finally:
            push_mod.httpx.post = post_originale
        check("Expo invia chunk da al massimo 100 e associa i ticket al chunk giusto",
              (notificati, [len(payload) for payload in payload_expo]),
              (205, [100, 100, 5]))
        check("i token Expo morti nei chunk 2 e 3 sono cancellati",
              seen_by_other("SELECT count(*) FROM user_push_tokens "
                            "WHERE user_id = %s AND token = ANY(%s)",
                            (uid_uso, list(morti_attesi))), 0)
        check("i ticket morti non cancellano il token con lo stesso indice nel chunk 1",
              seen_by_other("SELECT count(*) FROM user_push_tokens "
                            "WHERE user_id = %s AND token = %s",
                            (uid_uso, token_validi[0])), 1)
        check("un token non Expo non viene mai inviato",
              any(messaggio["to"] == "token-non-expo"
                  for payload in payload_expo for messaggio in payload), False)

        with work.cursor() as cur:
            cur.execute("UPDATE users SET delivery_channels = '{email}' WHERE id = %s",
                        (uid_uso,))
        work.commit()
        chiamate_inattese = []
        push_mod.httpx.post = lambda *a, **kw: chiamate_inattese.append(1)
        try:
            inattivi = push_mod.invia_digest_pronto(work, uid_uso)
        finally:
            push_mod.httpx.post = post_originale
        check("nessuna push se il canale app non è attivo",
              (inattivi, chiamate_inattese), (0, []))

        with work.cursor() as cur:
            cur.execute("UPDATE users SET delivery_channels = '{app}' WHERE id = %s",
                        (uid_uso,))
        work.commit()

        def post_errore(*a, **kw):
            raise RuntimeError("Expo HTTP finto")

        push_mod.httpx.post = post_errore
        try:
            errori_push = push_mod.invia_digest_pronto(work, uid_uso)
        finally:
            push_mod.httpx.post = post_originale
            with work.cursor() as cur:
                cur.execute("DELETE FROM model_pricing WHERE model IN "
                            "('locale-reale-finto')")
            work.commit()
        check("errori HTTP Expo ritornano zero senza toccare il digest",
              (errori_push,
               seen_by_other("SELECT status FROM digests WHERE user_id = %s",
                             (uid_uso,))), (0, "sent"))

        check_budget_provider(work, seen_by_other)

        section("autenticazione senza password")
        wipe(work)
        email_inviati: list[tuple] = []

        def cattura(destinatario, oggetto, testo, html):
            email_inviati.append((destinatario, oggetto, testo))

        token = auth.richiedi_magic_link(work, "Nuovo@Esempio.test", invia=cattura)
        check("richiesta link su email nuova = registrazione: l'utente esiste",
              seen_by_other("SELECT count(*) FROM users WHERE email = 'nuovo@esempio.test'"), 1)
        check("l'email porta il link con il token",
              bool(token) and token in email_inviati[0][2], True)
        check("del token si conserva solo lo sha256, mai il chiaro",
              seen_by_other("SELECT token_hash FROM login_tokens lt JOIN users u "
                            "ON u.id = lt.user_id WHERE u.email = 'nuovo@esempio.test'"),
              hashlib.sha256(token.encode()).hexdigest())

        r = auth.consuma(work, token)
        check("il link si scambia con una sessione", bool(r), True)
        sessione, uid = r
        check("il consumo prova l'email: verified valorizzato",
              seen_by_other("SELECT email_verified_at IS NOT NULL FROM users "
                            "WHERE email = 'nuovo@esempio.test'"), True)
        check("il link è monouso", auth.consuma(work, token), None)
        check("la sessione identifica l'utente",
              auth.verifica_sessione(work, sessione), uid)
        check("un token di sessione fasullo non entra",
              auth.verifica_sessione(work, "token-fasullo"), None)
        check("la sessione si revoca", auth.revoca_sessione(work, sessione), True)
        check("dopo la revoca non vale più",
              auth.verifica_sessione(work, sessione), None)

        # Rate limit: tre link nella finestra, il quarto si trova il muro.
        auth.richiedi_magic_link(work, "nuovo@esempio.test", invia=cattura)
        t3 = auth.richiedi_magic_link(work, "nuovo@esempio.test", invia=cattura)
        t4 = auth.richiedi_magic_link(work, "nuovo@esempio.test", invia=cattura)
        check("il terzo link nella finestra è ancora concesso", t3 is not None, True)
        check("il quarto link nella finestra è rifiutato", t4, None)

        # Scadenza: il link morto non entra, anche se mai consumato.
        with work.cursor() as cur:
            cur.execute("UPDATE login_tokens SET created_at = now() - interval '30 minutes', "
                        "  expires_at = now() - interval '1 minute' WHERE token_hash = %s",
                        (hashlib.sha256(t3.encode()).hexdigest(),))
        work.commit()
        check("il link scaduto è rifiutato", auth.consuma(work, t3), None)

        section("cifratura a busta dei CV")
        _os.environ["CV_KEK"] = _secrets.token_hex(32)
        segreto = "Curriculum di Giuseppe, HR Business Partner, 8 anni".encode()
        esito = crypto.cifra(segreto)
        check("il roundtrip cifra→decifra restituisce il file originale",
              crypto.decifra(esito.dati, esito.encrypted_dek), segreto)
        check("ciò che va in storage non contiene il testo in chiaro",
              segreto in esito.dati, False)
        check("le forme rispettano lo schema della 0009",
              (len(esito.nonce), len(esito.auth_tag), len(esito.encrypted_dek)),
              (12, 16, 60))
        check("due cifrature dello stesso file non si assomigliano",
              crypto.cifra(segreto).dati != esito.dati, True)
        try:
            crypto.decifra(esito.dati[:-1] + bytes([esito.dati[-1] ^ 1]),
                           esito.encrypted_dek)
            bad("un file manomesso viene rifiutato", "nessuna eccezione")
        except Exception:  # noqa: BLE001
            ok("un file manomesso viene rifiutato (GCM)", "InvalidTag")
        _os.environ["CV_KEK"] = _secrets.token_hex(32)   # KEK diversa
        try:
            crypto.decifra(esito.dati, esito.encrypted_dek)
            bad("una DEK avvolta con un'altra KEK non si apre", "nessuna eccezione")
        except Exception:  # noqa: BLE001
            ok("una DEK avvolta con un'altra KEK non si apre", "InvalidTag")
        del _os.environ["CV_KEK"]
        try:
            crypto.cifra(b"x")
            bad("senza KEK si rifiuta, non cifra con niente", "nessuna eccezione")
        except RuntimeError:
            ok("senza KEK si rifiuta, non cifra con niente", "RuntimeError")

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
