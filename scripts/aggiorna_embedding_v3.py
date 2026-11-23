#!/usr/bin/env python3
"""Backfill incrementale degli embedding E5, con upsert e copertura misurata."""
from __future__ import annotations

import argparse
import hashlib

import psycopg
from psycopg.rows import dict_row

from nivult.config import database_name, database_url
from nivult.matching.prescore_v3 import embed_batch
from nivult.matching.retrieval import HASH_DOCUMENTO_SQL, documento_ruolo

MODELLO = "intfloat/multilingual-e5-large-instruct"


def hash_offerta(offerta: dict) -> str:
    return hashlib.sha256(documento_ruolo(offerta).encode()).hexdigest()


def _connessione(produzione: bool = False):
    dsn = database_url()
    nome = database_name(dsn)
    if not produzione and not nome.endswith(("_test", "_dev")):
        raise SystemExit(f"database non di test: {nome}; usa --produzione solo dopo i gate")
    return psycopg.connect(dsn)


def _campi() -> str:
    return """j.id, j.title, j.organization, j.cities, j.countries, j.source,
              j.url, j.link_kind, j.employer_kind, j.date_posted, j.salary,
              j.ai_job_language, j.ai_experience_level, j.ai_work_arrangement,
              j.ai_employment_type, j.ai_visa_sponsorship, j.ai_key_skills,
              j.ai_requirements_summary, j.ai_core_responsibilities"""


_HASH_SQL = HASH_DOCUMENTO_SQL


def carica_lotto(conn, limite: int) -> list[dict]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(f"""SELECT {_campi()} FROM jobs j
            LEFT JOIN job_embeddings e ON e.job_id = j.id AND e.model = %s
            WHERE j.status = 'active' AND j.duplicate_of_job_id IS NULL
              AND (e.job_id IS NULL OR e.content_hash <> {_HASH_SQL})
            ORDER BY j.date_posted DESC LIMIT %s""", (MODELLO, limite))
        return cur.fetchall()


def copertura(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(f"""SELECT count(*) AS totale,
                count(e.job_id) FILTER (WHERE e.content_hash = {_HASH_SQL}) AS coperte
            FROM jobs j LEFT JOIN job_embeddings e ON e.job_id = j.id AND e.model = %s
            WHERE j.status = 'active' AND j.duplicate_of_job_id IS NULL""", (MODELLO,))
        totale, coperte = cur.fetchone()
    return {"totale": totale, "coperte": coperte,
            "quota": coperte / totale if totale else 1.0}


def aggiorna(conn, limite: int, massimo: int, dry_run: bool = False,
             embed_fn=embed_batch) -> int:
    inserite = 0
    while massimo <= 0 or inserite < massimo:
        righe = carica_lotto(conn, min(limite, massimo - inserite) if massimo > 0 else limite)
        if not righe:
            break
        vettori = embed_fn(righe)
        if len(vettori) != len(righe):
            raise RuntimeError("numero di embedding non allineato al batch")
        if not dry_run:
            with conn.cursor() as cur:
                for offerta, vettore in zip(righe, vettori):
                    cur.execute("""INSERT INTO job_embeddings
                        (job_id, model, content_hash, embedding)
                        VALUES (%s, %s, %s, %s::vector)
                        ON CONFLICT (job_id, model) DO UPDATE SET
                          content_hash = EXCLUDED.content_hash,
                          embedding = EXCLUDED.embedding,
                          embedded_at = now()""",
                        (offerta["id"], MODELLO, hash_offerta(offerta), vettore))
            conn.commit()
        inserite += len(righe)
        print("batch", inserite, copertura(conn), flush=True)
        if dry_run:
            break
    return inserite


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limite", type=int, default=256)
    ap.add_argument("--massimo", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--produzione", action="store_true")
    args = ap.parse_args()
    if args.limite <= 0 or args.limite > 256 or args.massimo < 0:
        raise SystemExit("limite non valido: 1-256; massimo >= 0")
    with _connessione(args.produzione) as conn:
        n = aggiorna(conn, args.limite, args.massimo, args.dry_run)
        print("completato", n, copertura(conn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
