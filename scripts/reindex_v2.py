#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from nivult.api_clienti import vivo
from nivult.ats.jsonld import _dsn
from opensearch_sync import _documenti, _righe

vivo._DSN = _dsn()

URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")
INDEX = "nivult_jobs_v2"
MAPPING = ROOT / "deploy/opensearch/jobs-index-v2.json"
STATE = Path("/opt/nivult/state/reindex_v2_state.json")
BATCH = 1500
WORKERS = 3


def _save(stato: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(stato, indent=2, default=str) + "\n")
    os.replace(tmp, STATE)


def _attendi() -> None:
    while True:
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
        time.sleep(10)


def _bulk(client: httpx.Client, documenti: list[dict]) -> int:
    if not documenti:
        return 0
    linee = []
    for doc in documenti:
        linee.append(json.dumps({"index": {"_index": INDEX, "_id": doc["id"]}}))
        linee.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=str))
    r = client.post("/_bulk", params={"filter_path": "errors,items.*.error"},
                    content=("\n".join(linee) + "\n").encode(),
                    headers={"Content-Type": "application/x-ndjson"})
    r.raise_for_status()
    esito = r.json()
    errori = [item for item in esito.get("items", [])
              for dettaglio in item.values() if "error" in dettaglio]
    if errori:
        print("errori documento", json.dumps(errori[:3], default=str)[:3000], flush=True)
    return len(errori)


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
    atteso = 1000
    totale = _count(client)
    if totale != atteso:
        raise RuntimeError(f"mini-test: attesi {atteso} doc, trovati {totale}")
    r = client.get(f"/{INDEX}/_mapping")
    r.raise_for_status()
    proprieta = r.json()[INDEX]["mappings"]["properties"]
    nuovi = {"source_type", "company", "company_slug", "company_domain", "city", "state",
             "employment_type", "department", "skills", "has_salary", "salary_min",
             "salary_max", "salary_currency"}
    if not nuovi <= set(proprieta):
        raise RuntimeError(f"campi mancanti nel mapping: {sorted(nuovi - set(proprieta))}")
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mini-test", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--finalize", action="store_true")
    args = ap.parse_args()
    massimo = 1000 if args.mini_test else None
    batch = massimo or BATCH
    if massimo and not args.finalize:
        args.finalize = False

    with httpx.Client(base_url=URL, timeout=httpx.Timeout(30.0, connect=3.0),
                      limits=httpx.Limits(max_connections=8, max_keepalive_connections=8)) as client:
        if args.mini_test:
            r = client.delete(INDEX)
            if r.status_code not in (200, 404):
                r.raise_for_status()
            r = client.put(INDEX, json=json.loads(MAPPING.read_text()))
            r.raise_for_status()
        if STATE.exists() and args.resume:
            stato = json.loads(STATE.read_text())
        else:
            import psycopg

            with psycopg.connect(vivo._DSN, autocommit=True, connect_timeout=3) as conn:
                t0 = conn.execute("SELECT now()").fetchone()[0].isoformat()
            stato = {"t0": t0, "last_id": None, "indexed": 0, "errors": 0,
                     "status": "running", "started_at": time.time()}
            _save(stato)
        inizio = time.monotonic()
        ultimo = stato.get("last_id") if args.resume else None
        pending = deque()
        finito = False
        errori = int(stato.get("errors", 0))
        indicizzati = int(stato.get("indexed", 0))
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            while not finito or pending:
                while not finito and len(pending) < WORKERS:
                    _attendi()
                    righe = _righe(cursore=ultimo, limite=batch)
                    if not righe:
                        finito = True
                        break
                    ultimo = righe[-1]["id"]
                    documenti = list(_documenti(righe).values())
                    if massimo and indicizzati + sum(n for _, n, _ in pending) >= massimo:
                        finito = True
                        break
                    pending.append((ultimo, len(documenti), pool.submit(_bulk, client, documenti)))
                if not pending:
                    break
                ultimo_batch, n, future = pending.popleft()
                errori += future.result()
                indicizzati += n
                stato.update(last_id=ultimo_batch, indexed=indicizzati, errors=errori,
                             duration_s=round(time.monotonic() - inizio, 1), status="running")
                _save(stato)
                velocita = indicizzati / max(time.monotonic() - inizio, 0.001)
                print(f"indexed={indicizzati} errors={errori} last_id={ultimo_batch} "
                      f"rate={velocita:.1f}/s", flush=True)
                if massimo and indicizzati >= massimo:
                    finito = True
                    pending.clear()
        if args.mini_test:
            _verifica(client)
            stato.update(status="mini_test_ok", doc_count=_count(client))
        elif args.finalize:
            _finalizza(client)
            stato.update(status="complete", doc_count=_count(client),
                         ignored=_ignored(client), duration_s=round(time.monotonic() - inizio, 1))
        else:
            stato.update(status="running", doc_count=_count(client))
        _save(stato)
        print("stato finale", json.dumps(stato, default=str), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
