#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from nivult.api_clienti import vivo
from nivult.ats.jsonld import _dsn
from opensearch_sync import _CANONICO, _documenti

vivo._DSN = _dsn()
URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")
INDEX = "nivult_jobs_v2"
MAPPING = ROOT / "deploy/opensearch/jobs-index-v2.json"
STATE = Path("/opt/nivult/state/reindex_v2_state.json")
BATCH = 1500
WORKERS = 3
MAX_UUID = (1 << 128) - 1
STATE_LOCK = threading.RLock()


def _uuid(valore) -> uuid.UUID:
    return valore if isinstance(valore, uuid.UUID) else uuid.UUID(str(valore))


def _normalizza_stato(stato: dict) -> dict:
    stato["last_id"] = _uuid(stato["last_id"])
    if stato.get("completed_prefix_until") is not None:
        stato["completed_prefix_until"] = _uuid(stato["completed_prefix_until"])
    for worker in stato.get("workers", []):
        for campo in ("start", "end", "cursor"):
            worker[campo] = _uuid(worker[campo])
    return stato


def _save(stato: dict) -> None:
    with STATE_LOCK:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(stato, indent=2, default=str) + "\n")
        os.replace(tmp, STATE)


def _attendi(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            prima = Path("/proc/pressure/io").read_text().splitlines()[0].split()
            io = float(dict(campo.split("=") for campo in prima[1:])["avg10"])
        except (OSError, ValueError, KeyError):
            io = 0.0
        try:
            meminfo = Path("/proc/meminfo").read_text().splitlines()
            disponibili = next(int(r.split()[1]) for r in meminfo
                               if r.startswith("MemAvailable:")) // 1024
        except (OSError, StopIteration, ValueError):
            disponibili = 4096
        if io <= 35.0 and disponibili >= 4096:
            return
        print(f"throttle: io={io:.1f}% MemAvailable={disponibili}MiB", flush=True)
        stop_event.wait(10)


def _bulk(client: httpx.Client, documenti: list[dict], stop_event: threading.Event) -> int:
    if not documenti:
        return 0
    linee = []
    for doc in documenti:
        doc = dict(doc)
        doc["id"] = str(_uuid(doc["id"]))
        linee.append(json.dumps({"index": {"_index": INDEX, "_id": doc["id"]}}))
        linee.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=str))
    for tentativo in range(7):
        r = client.post("/_bulk", params={"filter_path": "errors,items.*.error"},
                        content=("\n".join(linee) + "\n").encode(),
                        headers={"Content-Type": "application/x-ndjson"})
        if r.status_code == 429:
            if tentativo == 6:
                r.raise_for_status()
            attesa = min(2 ** tentativo, 30)
            print(f"HTTP 429 OpenSearch: backoff {attesa}s", flush=True)
            if stop_event.wait(attesa):
                raise RuntimeError("bulk interrotto durante il backoff 429")
            continue
        r.raise_for_status()
        esito = r.json()
        errori = [item for item in esito.get("items", [])
                  for dettaglio in item.values() if "error" in dettaglio]
        if errori:
            print("errori documento", json.dumps(errori[:3], default=str)[:3000], flush=True)
        return len(errori)
    raise RuntimeError("bulk OpenSearch fallito dopo 7 tentativi")


def _refresh(client: httpx.Client) -> None:
    r = client.post(f"/{INDEX}/_refresh")
    r.raise_for_status()


def _count(client: httpx.Client) -> int:
    r = client.get(f"/{INDEX}/_count")
    r.raise_for_status()
    return int(r.json()["count"])


def _ignored(client: httpx.Client) -> int:
    r = client.post(f"/{INDEX}/_count", json={"query": {"exists": {"field": "_ignored"}}})
    r.raise_for_status()
    return int(r.json()["count"])


def _finalizza(client: httpx.Client) -> None:
    r = client.get("/nivult_jobs_v1/_settings")
    r.raise_for_status()
    refresh = r.json()["nivult_jobs_v1"]["settings"]["index"]["refresh_interval"]
    r = client.put(f"/{INDEX}/_settings", json={"index": {"refresh_interval": refresh}})
    r.raise_for_status()
    _refresh(client)


def _verifica(client: httpx.Client) -> None:
    _refresh(client)
    totale = _count(client)
    if totale != 1000:
        raise RuntimeError(f"mini-test: attesi 1000 doc, trovati {totale}")
    r = client.get(f"/{INDEX}/_mapping")
    r.raise_for_status()
    proprieta = r.json()[INDEX]["mappings"]["properties"]
    nuovi = {"source_type", "company", "company_slug", "company_domain", "city", "state",
             "employment_type", "department", "skills", "has_salary", "salary_min",
             "salary_max", "salary_currency"}
    if not nuovi <= set(proprieta):
        raise RuntimeError(f"campi mancanti: {sorted(nuovi - set(proprieta))}")
    r = client.post(f"/{INDEX}/_search", json={
        "size": 1, "query": {"exists": {"field": "source_type"}},
        "_source": ["id", "title", "ats", "company", "company_slug", "company_domain",
                    "source_type", "city", "state", "employment_type", "department",
                    "skills", "has_salary", "salary_min", "salary_max", "salary_currency"]})
    r.raise_for_status()
    hits = r.json()["hits"]["hits"]
    if not hits:
        raise RuntimeError("mini-test: nessun documento con source_type")
    doc = hits[0]["_source"]
    for campo in ("id", "title", "ats", "company_slug", "source_type", "has_salary"):
        if campo not in doc:
            raise RuntimeError(f"campo obbligatorio assente: {campo}")
    print("mini-test documento", json.dumps(doc, ensure_ascii=False, default=str)[:3000],
          flush=True)
    print("mini-test ignored", _ignored(client), flush=True)


def _query_batch(conn, inizio: uuid.UUID, fine: uuid.UUID, limite: int):
    sql = f"""WITH batch AS MATERIALIZED (
        SELECT j.* FROM ats_jobs j
         WHERE {_CANONICO}
           AND j.id > %(inizio)s::uuid AND j.id <= %(fine)s::uuid
         ORDER BY j.id LIMIT {limite})
        SELECT {vivo._COLONNE.replace('FROM ats_jobs j', 'FROM batch j')}"""
    return conn.execute(sql, {"inizio": _uuid(inizio), "fine": _uuid(fine)}).fetchall()


def _worker(stato: dict, indice: int, inizio_tempo: float, stop_event: threading.Event,
            massimo: int | None = None, batch_size: int = BATCH) -> None:
    try:
        with httpx.Client(base_url=URL, timeout=httpx.Timeout(30.0, connect=3.0),
                          limits=httpx.Limits(max_connections=4, max_keepalive_connections=4)) as client, \
                psycopg.connect(vivo._DSN, autocommit=True, row_factory=dict_row,
                                connect_timeout=3) as conn:
            conn.execute("SET random_page_cost = 1.1")
            conn.execute("SET effective_io_concurrency = 200")
            conn.execute("SET max_parallel_workers_per_gather = 0")
            conn.execute("SET enable_mergejoin = off")
            conn.execute("SET statement_timeout = '60s'")
            while not stop_event.is_set() and not stato["workers"][indice]["done"]:
                _attendi(stop_event)
                if stop_event.is_set():
                    break
                corrente = stato["workers"][indice]
                righe = _query_batch(conn, corrente["cursor"], corrente["end"], batch_size)
                if not righe:
                    with STATE_LOCK:
                        corrente["done"] = True
                        _save(stato)
                    break
                nuovo_cursor = _uuid(righe[-1]["id"])
                documenti = list(_documenti(righe).values())
                errori = _bulk(client, documenti, stop_event)
                with STATE_LOCK:
                    corrente["cursor"] = nuovo_cursor
                    corrente["indexed"] = corrente.get("indexed", 0) + len(documenti)
                    corrente["errors"] = corrente.get("errors", 0) + errori
                    stato["indexed"] += len(documenti)
                    stato["errors"] += errori
                    stato["last_id"] = min(_uuid(w["cursor"]) for w in stato["workers"])
                    stato["duration_s"] = round(time.monotonic() - inizio_tempo, 1)
                    _save(stato)
                    velocita = ((stato["indexed"] - stato["run_indexed_start"])
                                / max(time.monotonic() - inizio_tempo, 0.001))
                    print(f"worker={indice} indexed={stato['indexed']} errors={stato['errors']} "
                          f"cursor={corrente['cursor']} aggregate_rate={velocita:.1f}/s", flush=True)
                    if massimo and stato["indexed"] >= massimo:
                        for worker in stato["workers"]:
                            worker["done"] = True
                        _save(stato)
                        break
    except Exception:
        traceback.print_exc()
        stop_event.set()
        raise


def _partizioni(last_id: uuid.UUID) -> list[dict]:
    inizio = _uuid(last_id).int
    intervallo = (MAX_UUID - inizio) // WORKERS
    confini = [inizio + intervallo * i for i in range(WORKERS)] + [MAX_UUID]
    return [{"worker": i, "start": uuid.UUID(int=confini[i]),
             "end": uuid.UUID(int=confini[i + 1]),
             "cursor": uuid.UUID(int=confini[i]), "done": False,
             "indexed": 0, "errors": 0} for i in range(WORKERS)]


def _stato_iniziale(resume: bool) -> dict:
    if STATE.exists() and resume:
        stato = json.loads(STATE.read_text())
        stato["workers"] = stato.get("workers") or _partizioni(_uuid(stato["last_id"]))
        return _normalizza_stato(stato)
    with psycopg.connect(vivo._DSN, autocommit=True, connect_timeout=3) as conn:
        t0 = conn.execute("SELECT now()").fetchone()[0].isoformat()
    stato = {"t0": t0, "last_id": uuid.UUID(int=0), "indexed": 0, "errors": 0,
             "status": "running", "started_at": time.time()}
    stato["workers"] = _partizioni(stato["last_id"])
    _save(stato)
    return stato


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mini-test", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    args = ap.parse_args()
    stato = _stato_iniziale(args.resume)
    if args.resume and stato.get("last_id"):
        stato.setdefault("completed_prefix_until", stato["last_id"])
        stato["completed_prefix_until"] = _uuid(stato["completed_prefix_until"])
    if args.mini_test:
        with httpx.Client(base_url=URL, timeout=10) as client:
            r = client.delete(INDEX)
            if r.status_code not in (200, 404):
                r.raise_for_status()
            r = client.put(INDEX, json=json.loads(MAPPING.read_text()))
            r.raise_for_status()
        stato["indexed"] = 0
        stato["errors"] = 0
        stato["workers"] = stato["workers"][:1]
    inizio = time.monotonic()
    stato["run_indexed_start"] = stato["indexed"]
    stop_event = threading.Event()
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = [pool.submit(_worker, stato, i, inizio, stop_event,
                                   1000 if args.mini_test else None,
                                   1000 if args.mini_test else BATCH)
                       for i in range(len(stato["workers"]))]
            for future in futures:
                future.result()
    except Exception:
        stato["status"] = "failed"
        _save(stato)
        raise
    with httpx.Client(base_url=URL, timeout=30) as client:
        if args.mini_test:
            _verifica(client)
            stato["status"] = "mini_test_ok"
        elif args.finalize and all(w["done"] for w in stato["workers"]):
            _finalizza(client)
            stato["status"] = "complete"
        stato["doc_count"] = _count(client)
        stato["ignored"] = _ignored(client)
        stato["duration_s"] = round(time.monotonic() - inizio, 1)
        _save(stato)
        print("stato finale", json.dumps(stato, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
