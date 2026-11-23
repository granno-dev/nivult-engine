"""Backfill e sync shadow di OpenSearch; mai invocato dal router live."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path

import duckdb
import httpx
import psycopg

from nivult.api_clienti import dati, vivo

INDEX_NAME = os.environ.get("OPENSEARCH_INDEX_NAME", "nivult_jobs_v1")
INDEX = os.environ.get("OPENSEARCH_INDEX", "nivult_jobs_active")
URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")
MAPPING = Path(__file__).resolve().parents[1] / "deploy/opensearch/jobs-index.json"
BATCH = 500

_CANONICO = """j.expired_at IS NULL
    AND (j.posted_at IS NULL OR j.posted_at <= now())
    AND NOT EXISTS (SELECT 1 FROM ats_jobs d
        WHERE d.duplicate_key = j.duplicate_key
          AND j.duplicate_key IS NOT NULL AND d.expired_at IS NULL
          AND (COALESCE(d.created_at, d.posted_at, d.fetched_at), d.id)
            < (COALESCE(j.created_at, j.posted_at, j.fetched_at), j.id))"""


def client() -> httpx.Client:
    return httpx.Client(base_url=URL, timeout=20)


def crea_indice() -> None:
    with client() as c:
        r = c.put(f"/{INDEX_NAME}", json=json.loads(MAPPING.read_text()))
        r.raise_for_status()


def _documenti(righe: list[dict]) -> dict[str, dict]:
    ids = [str(r["id"]) for r in righe]
    fonti = vivo._fonti_per_righe(ids)
    out = {}
    for r in righe:
        r["job_sources"] = fonti.get(str(r["id"]))
        out[str(r["id"])] = vivo._riga_export(r)
    return out


def _righe(ids: list[str] | None = None, cursore: str | None = None,
           limite: int = BATCH, canonico: bool = True) -> list[dict]:
    dove = [_CANONICO] if canonico else []
    par = {"limite": limite}
    if ids is not None:
        if not ids:
            return []
        dove.append("j.id = ANY(%(ids)s::uuid[])")
        par["ids"] = ids
    if cursore:
        dove.append("j.id > %(last_id)s::uuid")
        par["last_id"] = cursore
    sql = f"SELECT {vivo._COLONNE} WHERE {' AND '.join(dove)}"
    if ids is None:
        sql += " ORDER BY j.id LIMIT %(limite)s"
    with vivo._conn() as con:
        return con.execute(sql, par).fetchall()


def _bulk(azioni: list[tuple[str, dict | None]]) -> None:
    if not azioni:
        return
    linee = []
    for operazione, doc in azioni:
        job_id = doc["id"] if doc else None
        if not job_id:
            raise ValueError("azione bulk senza id")
        linee.append(json.dumps({operazione: {"_index": INDEX, "_id": job_id}}))
        if operazione == "index":
            doc = dict(doc)
            for campo in ("posted_at", "first_seen", "last_seen", "updated_at", "valid_through"):
                if isinstance(doc.get(campo), str) and " " in doc[campo]:
                    doc[campo] = doc[campo].replace(" ", "T", 1)
            linee.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":"),
                                    default=vivo._iso))
    with client() as c:
        r = c.post("/_bulk", content=("\n".join(linee) + "\n").encode(),
                   headers={"Content-Type": "application/x-ndjson"})
        r.raise_for_status()
        esito = r.json()
        if esito.get("errors"):
            errori = []
            for item in esito.get("items", []):
                azione, dettaglio = next(iter(item.items()))
                if "error" in dettaglio and not (azione == "delete" and dettaglio.get("status") == 404):
                    errori.append(dettaglio)
            if errori:
                raise RuntimeError(f"bulk OpenSearch fallito: {errori[:3]}")


def _backfill_sql() -> int:
    totali = 0
    with vivo._conn() as con:
        con.execute("SET statement_timeout = '30min'")
    with psycopg.connect(vivo._DSN) as con:
        con.execute("SET statement_timeout = '30min'")
        con.execute(f"CREATE TEMP TABLE backfill_ids AS SELECT j.id FROM ats_jobs j WHERE {_CANONICO}")
        con.execute("CREATE INDEX backfill_ids_id_idx ON backfill_ids (id)")
        with con.cursor(name="nivult_backfill_ids") as cur:
            cur.execute("SELECT id FROM backfill_ids")
            while True:
                ids = [str(r[0]) for r in cur.fetchmany(BATCH)]
                if not ids:
                    break
                documenti = _documenti(_righe(ids=ids, canonico=False))
                _bulk([("index", doc) for doc in documenti.values()])
                totali += len(documenti)
                if totali % 10000 < BATCH:
                    print(f"backfill: {totali} documenti", flush=True)
    return totali


def _backfill_duckdb() -> int:
    totali = 0
    con = duckdb.connect(dati._percorso, read_only=True)
    try:
        con.execute("SET memory_limit = '3GB'")
        con.execute("SET threads = 4")
        tmp = os.path.join(os.path.dirname(os.path.abspath(dati._percorso)), "duckdb-tmp")
        con.execute("SET temp_directory = '" + tmp.replace("'", "''") + "'")
        lettore = con.execute("SELECT raw FROM offerte")
        while True:
            blocco = lettore.fetchmany(BATCH)
            if not blocco:
                break
            documenti = [dati._senza_pii(json.loads(r[0])) for r in blocco]
            _bulk([("index", doc) for doc in documenti])
            totali += len(documenti)
            if totali % 10000 < BATCH:
                print(f"backfill DuckDB: {totali} documenti", flush=True)
    finally:
        con.close()
    return totali


def _riconcilia(da: dt.datetime, worker: int = 0, workers: int = 1,
                gap_only: bool = False) -> int:
    totali = 0
    with vivo._conn() as con:
        con.execute("SET statement_timeout = '30min'")
    with psycopg.connect(vivo._DSN) as con:
        con.execute("SET statement_timeout = '30min'")
        partizione = (f" AND abs(('x' || substr(md5(id::text), 1, 8))::bit(32)::int) "
                      f" %% {workers} = {worker}" if workers > 1 else "")
        query = "SELECT id FROM ats_jobs_gap_20261119 WHERE true" if gap_only else """SELECT id FROM ats_jobs
            WHERE created_at >= %(da)s OR content_updated_at >= %(da)s
               OR expired_at >= %(da)s"""
        par = {"da": da}
        with con.cursor(name="nivult_riconcilia") as cur:
            cur.execute(query + partizione, par)
            while True:
                ids = [str(r[0]) for r in cur.fetchmany(BATCH)]
                if not ids:
                    break
                documenti = _documenti(_righe(ids=ids))
                azioni = [("index", documenti[job_id]) if job_id in documenti
                          else ("delete", {"id": job_id}) for job_id in ids]
                _bulk(azioni)
                totali += len(documenti)
                if totali % 10000 < BATCH:
                    print(f"riconciliazione: {totali} documenti", flush=True)
    return totali


def _snapshot_duckdb() -> dt.datetime:
    meta = dati._meta()
    giorno = dt.date.fromisoformat(meta["data_export"])
    return dt.datetime.combine(giorno, dt.time.min, tzinfo=dt.UTC)


def backfill() -> None:
    if os.path.isfile(dati._percorso):
        totali = _backfill_duckdb()
        snapshot = _snapshot_duckdb()
        totali += _riconcilia(snapshot)
    else:
        totali = _backfill_sql()
    with client() as c:
        r = c.put(f"/{INDEX}/_settings", json={"index": {"refresh_interval": "5s"}})
        r.raise_for_status()
        r = c.post(f"/{INDEX}/_refresh")
        r.raise_for_status()
    print(f"backfill completato: {totali} documenti", flush=True)


def _sync_batch() -> int:
    with vivo._conn() as con:
        con.execute("SET statement_timeout = '2min'")
        con.execute("SET idle_in_transaction_session_timeout = '30s'")
        righe = con.execute(
            "SELECT job_id::text, op FROM ats_jobs_search_outbox "
            "ORDER BY queued_at LIMIT %s FOR UPDATE SKIP LOCKED", (BATCH,)).fetchall()
        if not righe:
            con.rollback()
            return 0
        upsert_ids = [r["job_id"] for r in righe if r["op"] == "upsert"]
        documenti = _documenti(_righe(ids=upsert_ids)) if upsert_ids else {}
        azioni = []
        for r in righe:
            job_id, op = r["job_id"], r["op"]
            if op == "delete" or job_id not in documenti:
                azioni.append(("delete", {"id": job_id}))
            else:
                azioni.append(("index", documenti[job_id]))
        _bulk(azioni)
        con.execute("SET LOCAL synchronous_commit = 'off'")
        con.execute("DELETE FROM ats_jobs_search_outbox WHERE job_id = ANY(%s::uuid[])",
                    ([r["job_id"] for r in righe],))
    return len(righe)


def sync(once: bool = False) -> None:
    with vivo._conn() as con:
        con.execute("SET statement_timeout = '2min'")
    while True:
        n = _sync_batch()
        if once:
            return
        if not n:
            time.sleep(2)


def _query(filtri: dict) -> dict:
    bool_query = {"filter": []}
    for campo in ("country", "category", "ats", "seniority", "language", "remote"):
        if filtri.get(campo):
            bool_query["filter"].append({"term": {campo: str(filtri[campo]).lower()}})
    if filtri.get("technology"):
        bool_query["filter"].append({"term": {"technologies": filtri["technology"].lower()}})
    if filtri.get("dal"):
        bool_query["filter"].append({"range": {"posted_at": {"gte": filtri["dal"]}}})
    if filtri.get("_null_posted"):
        bool_query["must_not"] = [{"exists": {"field": "posted_at"}}]
    if filtri.get("q"):
        bool_query["must"] = [{"multi_match": {
            "query": filtri["q"], "fields": ["title^2", "description"],
            "operator": "and", "type": "best_fields"}}]
    return {"bool": bool_query}


def cerca_jobs(filtri: dict, cursore: list | None = None, limite: int = 25,
               profilo: bool = False) -> dict:
    body = {"query": _query(filtri), "track_total_hits": False,
            "size": limite,
            "sort": [{"posted_at": {"order": "desc", "missing": "_last"}},
                     {"id": {"order": "desc", "missing": "_last"}}],
            "_source": True}
    if cursore:
        body["search_after"] = cursore
    if profilo:
        body["profile"] = True
    with client() as c:
        r = c.post(f"/{INDEX}/_search", json=body)
        r.raise_for_status()
        esito = r.json()
        hits = esito["hits"]["hits"]
    return {"data": [h["_source"] for h in hits],
            "next_cursor": hits[-1]["sort"] if len(hits) == limite else None,
            "count_page": len(hits), "profile": esito.get("profile")}


def conteggio_jobs(filtri: dict) -> int:
    with client() as c:
        r = c.post(f"/{INDEX}/_count", json={"query": _query(filtri)})
        r.raise_for_status()
        return int(r.json()["count"])


def main() -> int:
    ap = argparse.ArgumentParser()
    gruppo = ap.add_mutually_exclusive_group(required=True)
    gruppo.add_argument("--create-index", action="store_true")
    gruppo.add_argument("--backfill", action="store_true")
    gruppo.add_argument("--sync", action="store_true")
    gruppo.add_argument("--reconcile", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--worker", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--gap-only", action="store_true")
    args = ap.parse_args()
    if args.worker < 0 or args.workers < 1 or args.worker >= args.workers:
        ap.error("worker deve essere 0 <= worker < workers")
    if args.create_index:
        crea_indice()
    elif args.backfill:
        backfill()
    elif args.reconcile:
        da = _snapshot_duckdb()
        n = _riconcilia(da, args.worker, args.workers, args.gap_only)
        with client() as c:
            c.put(f"/{INDEX}/_settings", json={"index": {"refresh_interval": "5s"}}).raise_for_status()
            c.post(f"/{INDEX}/_refresh").raise_for_status()
        print(f"riconciliazione completata: {n} documenti", flush=True)
    else:
        sync(args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
