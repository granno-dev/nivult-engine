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
]

INDEXES = [
    "users_email_key", "users_due_idx",
    "user_cvs_one_active_idx", "user_cvs_user_idx",
    "clusters_active_idx", "cluster_daily_budget_open_idx",
    "user_clusters_by_cluster_idx",
    "jobs_active_posted_idx", "jobs_taxonomies_idx", "jobs_countries_idx",
    "jobs_key_skills_idx", "jobs_keywords_idx", "jobs_tsv_idx",
    "jobs_filters_idx", "jobs_last_seen_idx", "jobs_fingerprint_idx",
    "jobs_duplicate_of_idx", "jobs_purgeable_idx", "job_embeddings_hnsw_idx",
    "job_clusters_by_cluster_idx",
    "matches_user_recent_idx", "matches_passed_idx", "matches_job_idx",
    "digests_user_recent_idx", "digests_pending_idx",
    "digest_items_job_idx", "digest_items_user_idx",
    "ingestion_runs_cluster_idx", "ingestion_runs_running_idx",
    "api_usage_cluster_idx", "api_usage_user_idx", "api_usage_provider_idx",
    "api_usage_time_idx",
    "deletion_requests_open_idx", "deletion_requests_status_idx",
]

CONSTRAINTS = [
    "users_channel_address_ck", "users_schedule_ck", "users_deleted_ck",
    "user_cvs_embedding_ck",
    "clusters_family_country_key", "cluster_daily_budget_circuit_ck",
    "jobs_source_id_key", "jobs_canonical_url_key", "jobs_expiry_ck",
    "jobs_not_self_dup_ck",
    "matches_user_job_key", "matches_id_user_key",
    "digests_user_slot_key", "digests_id_user_key", "digests_sent_ck",
    "digests_empty_ck", "digests_counts_ck", "digests_period_ck",
    "digest_items_rank_key", "digest_items_digest_fk", "digest_items_match_fk",
    "ingestion_runs_finished_ck", "ingestion_runs_failed_ck", "job_clusters_run_fk",
    "deletion_requests_completed_ck", "deletion_requests_failed_ck",
    "jobs_raw_present_ck", "jobs_purged_is_dead_ck", "cluster_month_stats_month_ck",
]

FUNCTIONS = [
    "set_updated_at", "assert_valid_timezone", "cluster_try_consume",
    "assert_seniority_order", "jobs_derive_fields",
    "assert_duplicate_target_is_canonical", "delete_user_batch", "purge_dead_jobs",
]

TRIGGERS = [
    "users_set_updated_at", "users_valid_timezone", "clusters_set_updated_at",
    "user_clusters_seniority_order", "jobs_derive_fields_trg",
    "jobs_duplicate_target_canonical",
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
