#!/usr/bin/env python3
"""Contratti cache e ANN match-v3 su database _test/_dev, senza rete."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import psycopg
from psycopg.rows import dict_row

from aggiorna_embedding_v3 import _HASH_SQL, aggiorna, copertura, hash_offerta
from check_modules import seed_digest, wipe
from nivult.config import database_name, database_url
from nivult.matching import funnel

PASSATI = 0
FALLITI = 0


def check(nome: str, condizione: bool) -> None:
    global PASSATI, FALLITI
    if condizione:
        PASSATI += 1
        print("  ok   ", nome)
    else:
        FALLITI += 1
        print("  FAIL ", nome)


def prova_scala_e_lingua(conn, user_id: str) -> None:
    import json

    with conn.cursor() as cur:
        cur.execute("""INSERT INTO jobs
            (source, source_job_id, url, canonical_url, domain_derived,
             title, title_normalized, organization, date_posted, countries,
             ai_job_language, ai_experience_level, ai_employment_type,
             ai_key_skills, ai_requirements_summary, ai_core_responsibilities,
             raw, link_kind)
            SELECT 'fantastic', 'bulk-' || n, 'https://acme.example/bulk-' || n,
                   'https://acme.example/bulk-' || n, 'acme.example',
                   'Offerta scala ' || n, 'offerta scala ' || n, 'Acme', now(), ARRAY['IT'],
                   'Italian', '5-10', 'FULL_TIME', ARRAY['recruiting'],
                   CASE WHEN n >= 1200 THEN 'Language Requirement: Swedish (Mandatory)'
                        ELSE 'requisiti finti' END,
                   'responsabilità finte', '{}'::jsonb, 'career_site'
            FROM generate_series(0, 1600) AS n""")
        cur.execute("INSERT INTO job_clusters (job_id, cluster_id) "
                    "SELECT id, %s FROM jobs WHERE source_job_id LIKE 'bulk-%%'",
                    (ids_cluster(conn),))
    conn.commit()

    def embedding_scala(righe):
        out = []
        for r in righe:
            if not r["title"].startswith("Offerta scala "):
                out.append([0.01] * 1024)
                continue
            n = int(r["title"].rsplit(" ", 1)[-1])
            score = (n + 1) / 1602
            vettore = [0.0] * 1024
            vettore[0] = score
            vettore[1] = (1 - score * score) ** 0.5
            out.append(vettore)
        return out

    aggiorna(conn, 256, 0, embed_fn=embedding_scala)
    check("secondo backfill su larga scala non ricalcola",
          aggiorna(conn, 256, 0, embed_fn=embedding_scala) == 0)
    check("banco ANN oltre 1500 offerte", len(funnel.candidati(conn, user_id)) > 1500)
    baseline = funnel.candidati(conn, user_id, lingue_profilo={"Italian"})
    ann = funnel.candidati(conn, user_id, vettore=[1.0] + [0.0] * 1023,
                           lingue_profilo={"Italian"})

    def score(r):
        if not r["source_job_id"].startswith("bulk-"):
            return 0.01 / (1024 * 0.0001) ** 0.5
        return (int(r["source_job_id"].split("-")[-1]) + 1) / 1602

    top_baseline = {r["id"] for r in sorted(
        baseline, key=lambda r: (-score(r), r["id"]))[:150]}
    top_ann = {r["id"] for r in sorted(ann, key=lambda r: (-score(r), r["id"]))[:150]}
    check("recall@150 ANN almeno pari al funnel completo", top_ann == top_baseline)
    check("overfetch continua dopo le lingue mancanti", len(ann) == 300)
    completo = funnel.candidati(conn, user_id, overfetch=1500, lingue_profilo={"Italian"})
    ripiego = funnel.candidati(conn, user_id, vettore=[1.0] + [0.0] * 1023,
                               overfetch=1500, lingue_profilo={"Italian"})
    check("fallback completo se overfetch non raggiungibile",
          {r["id"] for r in ripiego} == {r["id"] for r in completo})
    with conn.cursor() as cur, conn.transaction():
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("EXPLAIN (COSTS OFF) " + funnel._ANN_ID_SQL, {
            "model": funnel.MODELLO_EMBEDDING,
            "query_embedding": json.dumps([1.0] + [0.0] * 1023),
            "ann_limit": 300,
        })
        check("HNSW utilizzabile per il retrieval globale",
              any("job_embeddings_hnsw_idx" in r[0] for r in cur.fetchall()))


def ids_cluster(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM clusters LIMIT 1")
        return cur.fetchone()[0]


def main() -> int:
    if not database_name(database_url()).endswith(("_test", "_dev")):
        raise SystemExit("serve un database _test/_dev")
    with psycopg.connect(database_url()) as conn:
        wipe(conn)
        ids = seed_digest(conn)
        check("cache incompleta rilevata", funnel.copertura_embedding(conn, ids["a"]) == 0.0)

        def embedding_finto(righe):
            return [[0.01] * 1024 for _ in righe]

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM jobs WHERE status = 'active' AND duplicate_of_job_id IS NULL")
            attese = cur.fetchone()[0]
        inserite = aggiorna(conn, 2, 0, embed_fn=embedding_finto)
        check("backfill inserisce tutte le offerte attive", inserite == attese)
        check("copertura completa", copertura(conn)["quota"] == 1.0)
        check("copertura ANN completa", funnel.copertura_embedding(conn, ids["a"]) == 1.0)
        check("secondo backfill non duplica", aggiorna(conn, 2, 0, embed_fn=embedding_finto) == 0)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM job_embeddings WHERE model = %s",
                        ("intfloat/multilingual-e5-large-instruct",))
            check("una riga per offerta e modello", cur.fetchone()[0] == attese)
            cur.execute("UPDATE jobs SET title = title || ' revisione' "
                        "WHERE source_job_id = 'ok1'")
            conn.commit()
        check("cache obsoleta riduce la copertura globale", copertura(conn)["quota"] < 1.0)
        quota = funnel.copertura_embedding(conn, ids["a"])
        check("cache obsoleta riduce la copertura ANN", 0.0 < quota < 1.0)
        check("backfill aggiorna solo i vettori obsoleti",
              aggiorna(conn, 2, 0, embed_fn=embedding_finto) == 1)
        check("copertura ANN torna completa", funnel.copertura_embedding(conn, ids["a"]) == 1.0)
        normale = {r["id"] for r in funnel.candidati(conn, ids["a"])}
        ann = {r["id"] for r in funnel.candidati(conn, ids["a"], vettore=[0.01] * 1024)}
        check("ANN non perde offerte rispetto al funnel", ann == normale)
        try:
            funnel.candidati(conn, ids["a"], vettore=[0.1] * 3)
        except ValueError:
            check("vettore malformato rifiutato", True)
        else:
            check("vettore malformato rifiutato", False)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT title, ai_requirements_summary FROM jobs LIMIT 1")
            r = cur.fetchone()
            check("hash deterministico", hash_offerta(r) == hash_offerta(dict(r)))
            check("hash cambia col contenuto",
                  hash_offerta(r) != hash_offerta(dict(r, ai_requirements_summary="altro")))
            cur.execute("UPDATE jobs SET ai_core_responsibilities = NULL, ai_requirements_summary = NULL")
            conn.commit()
            cur.execute(f"SELECT title, {_HASH_SQL} AS sql_hash FROM jobs j LIMIT 1")
            r = cur.fetchone()
            check("hash SQL e Python identici con campi nulli", hash_offerta(r) == r["sql_hash"])
            cur.execute("UPDATE jobs SET ai_core_responsibilities = %s, "
                        "ai_requirements_summary = NULL", ("Contatto\u00a0clienti\n",))
            conn.commit()
            cur.execute(f"SELECT title, ai_core_responsibilities, {_HASH_SQL} AS sql_hash "
                        "FROM jobs j LIMIT 1")
            r = cur.fetchone()
            check("hash SQL e Python identici con NBSP e a capo finali",
                  hash_offerta(r) == r["sql_hash"])
            cur.execute("UPDATE jobs SET ai_core_responsibilities = ''")
            conn.commit()
            cur.execute(f"SELECT title, ai_core_responsibilities, {_HASH_SQL} AS sql_hash "
                        "FROM jobs j LIMIT 1")
            r = cur.fetchone()
            check("hash SQL e Python identici con testo vuoto", hash_offerta(r) == r["sql_hash"])
        prova_scala_e_lingua(conn, ids["a"])
        wipe(conn)
    print(f"\n{PASSATI} ok, {FALLITI} falliti")
    return 1 if FALLITI else 0


if __name__ == "__main__":
    raise SystemExit(main())
