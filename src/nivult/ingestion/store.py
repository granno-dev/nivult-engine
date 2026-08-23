"""Scrittura delle offerte nel database.

Isolata dal runner perché è la parte con le insidie: due vincoli UNIQUE sulla
stessa tabella, e offerte che risorgono.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

import psycopg

from nivult.ingestion.models import RawJob

log = logging.getLogger("nivult.ingestion.store")

_COLUMNS = (
    "source", "source_job_id", "url", "canonical_url", "link_kind",
    "domain_derived", "org_linkedin_slug", "title", "title_normalized",
    "organization", "date_posted", "cities", "countries", "locations",
    "ai_job_language", "ai_visa_sponsorship", "ai_work_arrangement",
    "ai_experience_level", "ai_employment_type", "ai_working_hours",
    "ai_key_skills", "ai_keywords", "ai_taxonomies_a",
    "ai_requirements_summary", "ai_core_responsibilities",
    "salary", "date_valid_through", "ai_education", "organization_logo",
    "ai_work_arrangement_office_days",
    "org_size", "org_headcount", "org_industry", "employer_agency_declared",
    "raw",
)

# Ciò che si aggiorna quando un'offerta già nota ricompare. Fuori restano
# canonical_url (è la chiave), first_seen_at, e tutto ciò che riguarda i
# duplicati: quella decisione la prende il passo di deduplica, non l'ingestione.
_MUTABLE = tuple(c for c in _COLUMNS if c not in ("source", "source_job_id", "canonical_url"))

_JSON_COLUMNS = {"locations", "salary", "raw"}


def _values(job: RawJob) -> list:
    d = asdict(job)
    return [json.dumps(d[c]) if c in _JSON_COLUMNS and d[c] is not None else d[c]
            for c in _COLUMNS]


def upsert_job(cur: psycopg.Cursor, job: RawJob) -> tuple[str, bool]:
    """Inserisce o aggiorna. Ritorna (job_id, è_nuova).

    Su jobs ci sono DUE vincoli UNIQUE — (source, source_job_id) e canonical_url —
    e ON CONFLICT ne può inseguire uno solo. Quindi prima si cerca per chiave
    della fonte: se l'offerta è già nostra la si aggiorna, anche se nel frattempo
    ha cambiato URL. Solo se non la conosciamo si tenta l'INSERT, dove il
    conflitto su canonical_url significa che la stessa offerta è già arrivata da
    un'altra fonte.
    """
    vals = _values(job)
    cur.execute(
        "SELECT id FROM jobs WHERE source = %s AND source_job_id = %s",
        (job.source, job.source_job_id),
    )
    row = cur.fetchone()

    if row:
        sets = ", ".join(f"{c} = %s" for c in _MUTABLE)
        cur.execute(
            f"UPDATE jobs SET {sets}, last_seen_alive_at = now(), "
            # Un'offerta che ricompare è viva di nuovo. Il vincolo
            # jobs_expiry_ck impone che expired_at torni NULL insieme allo stato.
            "    status = 'active', expired_at = NULL "
            "WHERE id = %s",
            [vals[_COLUMNS.index(c)] for c in _MUTABLE] + [row[0]],
        )
        return str(row[0]), False

    placeholders = ", ".join(["%s"] * len(_COLUMNS))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in _MUTABLE)
    cur.execute(
        f"INSERT INTO jobs ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT (canonical_url) DO UPDATE SET {updates}, "
        f"    last_seen_alive_at = now(), status = 'active', expired_at = NULL "
        # xmax = 0 solo per le righe davvero inserite: distingue INSERT da
        # UPDATE senza una seconda query.
        f"RETURNING id, (xmax = 0) AS inserted",
        vals,
    )
    job_id, inserted = cur.fetchone()
    return str(job_id), bool(inserted)


def link_to_cluster(cur: psycopg.Cursor, job_id: str, cluster_id: str, run_id: str) -> None:
    cur.execute(
        "INSERT INTO job_clusters (job_id, cluster_id, ingestion_run_id) "
        "VALUES (%s, %s, %s) ON CONFLICT (job_id, cluster_id) DO NOTHING",
        (job_id, cluster_id, run_id),
    )


def record_usage(cur: psycopg.Cursor, *, provider: str, cluster_id: str, run_id: str,
                 requests: int, credits: int, http_status: int | None,
                 latency_ms: int | None) -> None:
    """Anche le chiamate gratuite. Non per i soldi: per vedere una fonte che
    degrada prima che diventi un problema."""
    cur.execute(
        "INSERT INTO api_usage (provider, operation, cluster_id, ingestion_run_id, "
        "                       requests, credits, http_status, latency_ms) "
        "VALUES (%s, 'fetch', %s, %s, %s, %s, %s, %s)",
        (provider, cluster_id, run_id, requests, credits, http_status, latency_ms),
    )
