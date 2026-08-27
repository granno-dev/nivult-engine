#!/usr/bin/env python3
"""Verifica strutturale dello schema. Sola lettura: sicuro in produzione.

    python scripts/verify_schema.py

Esce con codice 1 se qualcosa manca. Controlla che ci sia ciò che le migrazioni
dovrebbero aver creato, che le impostazioni di database siano attive, e segnala
la deriva del vocabolario del fornitore.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import psycopg  # noqa: E402

from nivult.config import database_url, safe_dsn  # noqa: E402

TABLES = [
    "schema_migrations", "experience_levels", "users", "user_cvs",
    "clusters", "cluster_daily_budget", "user_clusters",
    "jobs", "job_embeddings", "job_clusters",
    "matches", "digests", "digest_items",
    "ingestion_runs", "api_usage", "deletion_requests", "cluster_month_stats",
    "login_tokens", "sessions", "oauth_identities", "link_kinds",
    "employer_kinds", "staffing_agency_patterns",
    "provider_quotas", "provider_budget", "job_families",
    "cluster_source_queries", "filter_values", "filter_bindings", "user_filters",
    "plan_quotas", "user_evaluation_budget",
    "cluster_source_cursors",
    "model_pricing", "benchmark_runs", "benchmark_models", "benchmark_scores",
]

INDEXES = [
    "users_email_key", "users_due_idx",
    "user_cvs_one_active_idx", "user_cvs_user_idx",
    "clusters_active_idx", "cluster_daily_budget_open_idx",
    "user_clusters_by_cluster_idx",
    "jobs_active_posted_idx", "jobs_taxonomies_idx", "jobs_countries_idx",
    "jobs_key_skills_idx", "jobs_keywords_idx", "jobs_tsv_idx",
    "jobs_filters_idx", "jobs_last_seen_idx", "jobs_fingerprint_idx",
    "jobs_duplicate_of_idx", "jobs_purgeable_idx", "jobs_link_kind_idx", "jobs_employer_kind_idx", "jobs_org_size_idx",
    "job_embeddings_hnsw_idx",
    "job_clusters_by_cluster_idx",
    "matches_user_recent_idx", "matches_passed_idx", "matches_job_idx",
    "digests_user_recent_idx", "digests_pending_idx",
    "digest_items_job_idx", "digest_items_user_idx",
    "ingestion_runs_cluster_idx", "ingestion_runs_running_idx",
    "api_usage_cluster_idx", "api_usage_user_idx", "api_usage_provider_idx",
    "api_usage_time_idx",
    "deletion_requests_open_idx", "deletion_requests_status_idx",
    "login_tokens_user_idx", "login_tokens_expiry_idx",
    "sessions_user_idx", "sessions_active_idx", "oauth_identities_user_idx",
]

CONSTRAINTS = [
    "users_channels_ck", "users_schedule_ck", "users_deleted_ck",
    "user_cvs_embedding_ck",
    "clusters_family_country_key", "clusters_family_fk", "cluster_daily_budget_circuit_ck",
    "jobs_source_id_key", "jobs_canonical_url_key", "jobs_expiry_ck",
    "jobs_not_self_dup_ck",
    "matches_user_job_key", "matches_id_user_key",
    "digests_user_slot_key", "digests_id_user_key", "digests_sent_ck",
    "digests_empty_ck", "digests_counts_ck", "digests_period_ck",
    "digest_items_rank_key", "digest_items_digest_fk", "digest_items_match_fk",
    "ingestion_runs_finished_ck", "ingestion_runs_failed_ck", "job_clusters_run_fk",
    "deletion_requests_completed_ck", "deletion_requests_failed_ck",
    "jobs_raw_present_ck", "jobs_purged_is_dead_ck", "cluster_month_stats_month_ck",
    "provider_budget_month_ck", "provider_budget_circuit_ck",
    "user_clusters_employer_kinds_ck",
    "cluster_source_cursors_source_check",
    "login_tokens_hash_key", "login_tokens_window_ck", "login_tokens_consumed_ck",
    "sessions_hash_key", "sessions_window_ck",
    "oauth_identities_user_provider_key", "user_cvs_encryption_ck",
    "employer_kinds_rank_key", "employer_kinds_rank_ck",
    "benchmark_models_run_model_key", "benchmark_scores_esito_ck",
]

FUNCTIONS = [
    "set_updated_at", "assert_valid_timezone", "cluster_try_consume",
    "assert_seniority_order", "jobs_derive_fields",
    "assert_duplicate_target_is_canonical", "delete_user_batch", "purge_dead_jobs", "purge_expired_auth",
    "normalize_org", "classify_employer", "jobs_set_employer_kind",
    "resolve_duplicates", "provider_try_consume", "settle_credits",
    "expire_stale_jobs", "mark_jobs_removed", "user_try_evaluate",
    "cluster_needs_prescreen",
    "assert_employer_kinds_valid", "cluster_try_consume_backfill",
    "cluster_finish_backfill",
    "reclassify_employers",
    "benchmark_recall",
]

TRIGGERS = [
    "users_set_updated_at", "users_valid_timezone", "clusters_set_updated_at",
    "user_clusters_seniority_order", "jobs_derive_fields_trg",
    "jobs_duplicate_target_canonical", "jobs_employer_kind_trg",
    "user_clusters_employer_kinds_trg",
]


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.warnings: list[str] = []

    def check(self, ok: bool, label: str, detail: str = "") -> None:
        if ok:
            print(f"  ok    {label}")
        else:
            print(f"  FAIL  {label}{(' — ' + detail) if detail else ''}")
            self.failures.append(label)

    def warn(self, label: str) -> None:
        print(f"  warn  {label}")
        self.warnings.append(label)

    def section(self, title: str) -> None:
        print(f"\n{title}")


def missing(cur, query: str, expected: list[str]) -> list[str]:
    cur.execute(query)
    present = {row[0] for row in cur.fetchall()}
    return [name for name in expected if name not in present]


def main() -> int:
    dsn = database_url()
    rep = Report()
    print(f"verifica schema — {safe_dsn(dsn)}")

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:

        rep.section("estensioni")
        cur.execute("SELECT extname, extversion FROM pg_extension")
        ext = dict(cur.fetchall())
        rep.check("citext" in ext, "citext installata")
        rep.check("vector" in ext, "pgvector installata")
        if "vector" in ext:
            version = ext["vector"]
            parts = tuple(int(p) for p in version.split(".")[:2] if p.isdigit())
            rep.check(
                parts >= (0, 8),
                f"pgvector >= 0.8 (trovata {version})",
                "hnsw.iterative_scan richiede 0.8: senza, la ricerca ANN "
                "filtrata per cluster perde recall",
            )

        rep.section("impostazioni di database")
        cur.execute("SHOW timezone")
        tz = cur.fetchone()[0]
        rep.check(tz == "UTC", f"timezone = UTC (trovato {tz})")
        try:
            cur.execute("SHOW hnsw.iterative_scan")
            scan = cur.fetchone()[0]
            rep.check(scan == "relaxed_order", f"hnsw.iterative_scan = relaxed_order (trovato {scan})")
        except psycopg.Error as exc:
            rep.check(False, "hnsw.iterative_scan leggibile", str(exc).strip())

        rep.section("tabelle")
        absent = missing(
            cur,
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'",
            TABLES,
        )
        rep.check(not absent, f"{len(TABLES)} tabelle attese", f"mancano: {', '.join(absent)}")

        rep.section("indici")
        absent = missing(
            cur,
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'",
            INDEXES,
        )
        rep.check(not absent, f"{len(INDEXES)} indici attesi", f"mancano: {', '.join(absent)}")

        cur.execute(
            "SELECT am.amname FROM pg_class c "
            "JOIN pg_am am ON am.oid = c.relam "
            "WHERE c.relname = 'job_embeddings_hnsw_idx'"
        )
        row = cur.fetchone()
        rep.check(row is not None and row[0] == "hnsw",
                  "job_embeddings_hnsw_idx usa l'access method hnsw",
                  f"trovato: {row[0] if row else 'assente'}")

        cur.execute("SELECT viewname FROM pg_views WHERE schemaname = 'public'")
        views = {r[0] for r in cur.fetchall()}
        rep.check("cluster_month_stats_v" in views, "vista cluster_month_stats_v")
        rep.check("provider_budget_v" in views, "vista provider_budget_v")
        rep.check("cluster_backfill_v" in views, "vista cluster_backfill_v")
        rep.check("cluster_coverage_v" in views, "vista cluster_coverage_v")
        rep.check("expiry_blind_spots_v" in views, "vista expiry_blind_spots_v")
        rep.check("cluster_volume_v" in views, "vista cluster_volume_v")
        rep.check("benchmark_models_v" in views, "vista benchmark_models_v")

        rep.section("vincoli")
        absent = missing(cur, "SELECT conname FROM pg_constraint", CONSTRAINTS)
        rep.check(not absent, f"{len(CONSTRAINTS)} vincoli attesi", f"mancano: {', '.join(absent)}")

        rep.section("funzioni e trigger")
        absent = missing(
            cur,
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public'",
            FUNCTIONS,
        )
        rep.check(not absent, f"{len(FUNCTIONS)} funzioni attese", f"mancano: {', '.join(absent)}")

        absent = missing(cur, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal", TRIGGERS)
        rep.check(not absent, f"{len(TRIGGERS)} trigger attesi", f"mancano: {', '.join(absent)}")

        rep.section("migrazioni")
        cur.execute("SELECT count(*), max(version) FROM schema_migrations")
        count, top = cur.fetchone()
        expected = len(list((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")))
        rep.check(count == expected,
                  f"{expected} migrazioni applicate (trovate {count}, ultima {top})")

        rep.section("politica sui link")
        cur.execute("SELECT kind || '=' || rank FROM link_kinds ORDER BY rank")
        kinds = [r[0] for r in cur.fetchall()]
        rep.check(kinds == ["career_site=1", "national_agency=2", "job_board=3"],
                  "ordine di preferenza dei tipi di link", f"trovato: {kinds}")
        cur.execute("SELECT count(*) FROM jobs j LEFT JOIN link_kinds k "
                    "ON k.kind = j.link_kind WHERE k.kind IS NULL")
        rep.check(cur.fetchone()[0] == 0, "ogni offerta ha un tipo di link noto")

        cur.execute("SELECT count(*) FROM staffing_agency_patterns")
        n_pat = cur.fetchone()[0]
        rep.check(n_pat > 0, f"lista agenzie popolata ({n_pat} pattern)")
        cur.execute("SELECT count(*) FROM jobs "
                    "WHERE employer_kind IS DISTINCT FROM classify_employer(organization)")
        drift = cur.fetchone()[0]
        if drift:
            rep.warn(f"{drift} offerte con etichetta datore disallineata dalla lista — "
                     f"eseguire SELECT reclassify_employers()")
        else:
            rep.check(True, "etichette datore allineate alla lista")

        cur.execute("SELECT count(*) FROM job_families")
        rep.check(cur.fetchone()[0] == 33, "33 famiglie professionali censite")

        cur.execute("SELECT count(*) FILTER (WHERE org_logo_permalink IS NOT NULL "
                    "                          OR organization_logo IS NOT NULL), count(*) "
                    "FROM jobs WHERE source = 'fantastic'")
        con, tot = cur.fetchone()
        if tot:
            print(f"  info  logo disponibile su {100*con//tot}% delle offerte Fantastic "
                  f"({con}/{tot})")

        rep.section("scadenze")
        cur.execute("SELECT coalesce(sum(offerte_non_giudicabili),0) FROM expiry_blind_spots_v")
        cieche = cur.fetchone()[0]
        if cieche:
            rep.warn(f"{cieche} offerte in cluster con fetch troncate: la scadenza "
                     f"non si può dedurre, restano attive finché la fonte non le dichiara")
        else:
            rep.check(True, "nessun punto cieco sulle scadenze")
        cur.execute("SELECT count(*) FROM jobs WHERE status='active' AND expired_at IS NOT NULL")
        rep.check(cur.fetchone()[0] == 0, "nessuna offerta attiva con data di scadenza")

        rep.section("vocabolari dei filtri")
        cur.execute("SELECT parameter, count(*), "
                    "       count(*) FILTER (WHERE evidence = 'verified') "
                    "FROM filter_values GROUP BY 1 ORDER BY 1")
        for par, tot, ver in cur.fetchall():
            print(f"  info  {par:<24} {ver}/{tot} valori verificati con una chiamata")

        # Un valore fuori vocabolario nei filtri utente non dà errore all'API:
        # dà zero risultati, e sembra che il mercato sia vuoto invece che la
        # query sbagliata. Qui deve fallire, rumorosamente.
        cur.execute("SELECT column_name, parameter FROM filter_bindings ORDER BY column_name")
        for colonna, parametro in cur.fetchall():
            cur.execute(
                f"SELECT count(*), string_agg(DISTINCT v, ', ') FROM ("
                f"  SELECT unnest({colonna}) AS v FROM user_clusters) x "
                f"WHERE v NOT IN (SELECT api_value FROM filter_values "
                f"                 WHERE parameter = %s)", (parametro,))
            n, esempi = cur.fetchone()
            rep.check(n == 0,
                      f"user_clusters.{colonna} usa solo valori di {parametro}",
                      f"{n} fuori vocabolario: {esempi} — l'API restituirebbe "
                      f"zero risultati SENZA errore")

        rep.section("valutazione")
        cur.execute("SELECT count(*) FROM plan_quotas")
        rep.check(cur.fetchone()[0] == 3, "quota di valutazione per ogni piano")
        cur.execute("SELECT u.plan FROM users u LEFT JOIN plan_quotas q ON q.plan = u.plan "
                    "WHERE q.plan IS NULL GROUP BY 1")
        senza = [r[0] for r in cur.fetchall()]
        rep.check(not senza, "ogni piano in uso ha una quota",
                  f"senza quota: {', '.join(senza)}")
        cur.execute("SELECT family || ' × ' || country || ' (' || offerte_30g || '/mese)' "
                    "FROM cluster_volume_v WHERE prescreening_attivo")
        grossi = [r[0] for r in cur.fetchall()]
        if grossi:
            rep.warn("pre-screening attivo su: " + "; ".join(grossi)
                     + " — valvola inserita, non un guasto")
        else:
            rep.check(True, "nessun cluster sopra la soglia di pre-screening")

        rep.section("portata dei cluster")
        cur.execute("SELECT family || ' × ' || country || ' (' || n || ' offerte)' "
                    "FROM (SELECT c.family, c.country, count(*) n FROM clusters c "
                    "  JOIN job_clusters jc ON jc.cluster_id = c.id "
                    "  GROUP BY c.id, c.family, c.country HAVING count(*) > 2000) x")
        larghi = [r[0] for r in cur.fetchall()]
        if larghi:
            rep.warn("cluster probabilmente troppo larghi: " + "; ".join(larghi)
                     + " — un cluster è famiglia × paese, non mezzo mercato")
        else:
            rep.check(True, "nessun cluster sospettosamente largo")

        cur.execute("SELECT family || ' × ' || country FROM clusters "
                    "WHERE backfill_truncated")
        troncati = [r[0] for r in cur.fetchall()]
        if troncati:
            rep.warn("backfill chiuso con dotazione esaurita, storico incompleto: "
                     + ", ".join(troncati))
        else:
            rep.check(True, "nessun backfill troncato")

        cur.execute("SELECT family || ' × ' || country || ' / ' || source "
                    "FROM cluster_coverage_v WHERE NOT interrogabile")
        scoperti = [r[0] for r in cur.fetchall()]
        if scoperti:
            rep.warn("cluster senza termine di ricerca, la fonte li salterà: "
                     + "; ".join(scoperti))
        else:
            rep.check(True, "ogni cluster è interrogabile da tutte le sue fonti")

        rep.section("budget dei fornitori")
        cur.execute("SELECT provider FROM provider_quotas ORDER BY provider")
        have = {r[0] for r in cur.fetchall()}
        # Ogni fonte da cui abbiamo ingerito deve avere una quota configurata,
        # o provider_try_consume solleva a runtime invece che qui.
        cur.execute("SELECT DISTINCT source FROM jobs")
        used = {r[0] for r in cur.fetchall()}
        senza_quota = sorted(used - have)
        rep.check(not senza_quota, "ogni fonte usata ha una quota configurata",
                  f"mancano: {', '.join(senza_quota)}")
        cur.execute("SELECT provider || ' ' || credits_used || '/' || monthly_credits_cap "
                    "FROM provider_budget_v WHERE monthly_credits_cap > 0")
        for row in cur.fetchall():
            print(f"  info  consumo mensile: {row[0]}")

        rep.section("autenticazione senza password")
        cur.execute(
            "SELECT table_name || '.' || column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND (column_name ILIKE '%password%' "
            "   OR column_name ILIKE '%passwd%' OR column_name ILIKE '%pwd%')")
        pw = [r[0] for r in cur.fetchall()]
        rep.check(not pw, "nessuna colonna password nello schema",
                  f"trovate: {', '.join(pw)}")

        cur.execute("SELECT count(*) FROM user_cvs WHERE length(encrypted_dek) < 32")
        rep.check(cur.fetchone()[0] == 0, "ogni CV ha una DEK avvolta")

        rep.section("deriva del vocabolario del fornitore")
        cur.execute(
            "SELECT DISTINCT j.ai_experience_level FROM jobs j "
            "LEFT JOIN experience_levels e ON e.code = j.ai_experience_level "
            "WHERE j.ai_experience_level IS NOT NULL AND e.code IS NULL"
        )
        unknown = [r[0] for r in cur.fetchall()]
        if unknown:
            rep.warn(
                f"ai_experience_level fuori vocabolario: {', '.join(unknown)} — "
                f"i filtri min/max di seniority NON li considerano. "
                f"Aggiornare experience_levels con una migrazione."
            )
        else:
            rep.check(True, "ai_experience_level tutti noti")

        rep.section("dimensioni")
        cur.execute(
            "SELECT c.relname, pg_size_pretty(pg_total_relation_size(c.oid)), "
            "       coalesce(s.n_live_tup, 0) "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid "
            "WHERE n.nspname = 'public' AND c.relkind = 'r' "
            "ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 8"
        )
        for name, size, rows in cur.fetchall():
            print(f"  {name:<24} {size:>10}  ~{rows} righe")

    print()
    if rep.failures:
        print(f"FALLITO: {len(rep.failures)} controlli non superati")
        for f in rep.failures:
            print(f"  - {f}")
        return 1
    if rep.warnings:
        print(f"OK con {len(rep.warnings)} avvisi")
        return 0
    print("OK: schema conforme")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
