#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import opensearch_sync
from nivult.api_clienti import vivo
from nivult.ats.jsonld import _dsn
from reindex_v2 import _attendi

vivo._DSN = _dsn()
opensearch_sync.INDEX = "nivult_jobs_v2"
STATE = Path("/opt/nivult/state/catchup_v2_state.json")
BATCH = 1500


def _uuid(valore) -> uuid.UUID:
    return valore if isinstance(valore, uuid.UUID) else uuid.UUID(str(valore))


def _save(stato: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(stato, indent=2, default=str) + "\n")
    os.replace(tmp, STATE)


def _batch_ids(conn, da: datetime, fine: datetime, cursor: uuid.UUID, limite: int = BATCH):
    sql = """WITH batch AS MATERIALIZED (
        SELECT j.id,j.expired_at FROM ats_jobs j
         WHERE j.id > %(cursor)s::uuid
           AND ((j.created_at >= %(da)s AND j.created_at <= %(fine)s)
                OR (j.content_updated_at >= %(da)s AND j.content_updated_at <= %(fine)s)
                OR (j.expired_at >= %(da)s AND j.expired_at <= %(fine)s))
         ORDER BY j.id LIMIT %(limite)s)
        SELECT id,expired_at FROM batch"""
    return conn.execute(sql, {"cursor": _uuid(cursor), "da": da, "fine": fine,
                              "limite": limite}).fetchall()


def _azioni(client: httpx.Client, azioni: list[tuple[str, dict]], stop_event: threading.Event) -> int:
    if not azioni:
        return 0
    linee = []
    for operazione, doc in azioni:
        job_id = str(_uuid(doc["id"]))
        linee.append(json.dumps({operazione: {"_index": "nivult_jobs_v2", "_id": job_id}}))
        if operazione == "index":
            linee.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":"), default=str))
    for tentativo in range(7):
        r = client.post("/_bulk", params={"filter_path": "errors,items.*.error,items.*.status"},
                        content=("\n".join(linee) + "\n").encode(),
                        headers={"Content-Type": "application/x-ndjson"})
        if r.status_code == 429:
            if tentativo == 6:
                r.raise_for_status()
            attesa = min(2 ** tentativo, 30)
            print(f"HTTP 429: backoff {attesa}s", flush=True)
            if stop_event.wait(attesa):
                raise RuntimeError("catch-up interrotto durante backoff")
            continue
        r.raise_for_status()
        errori = []
        for item in r.json().get("items", []):
            operazione, dettaglio = next(iter(item.items()))
            if "error" in dettaglio and not (operazione == "delete" and dettaglio.get("status") == 404):
                errori.append(dettaglio)
        if errori:
            print("errori bulk", json.dumps(errori[:3], default=str)[:3000], flush=True)
        return len(errori)
    raise RuntimeError("catch-up bulk fallito")


def run(da: datetime, fine: datetime) -> dict:
    stato = {"da": da.isoformat(), "fine": fine.isoformat(),
             "cursor": str(uuid.UUID(int=0)), "upsert": 0, "delete": 0, "errors": 0,
             "status": "running", "started_at": time.time()}
    _save(stato)
    inizio = time.monotonic()
    cursor = uuid.UUID(int=0)
    stop_event = threading.Event()
    with psycopg.connect(vivo._DSN, autocommit=True, connect_timeout=3,
                         row_factory=dict_row) as conn, \
            httpx.Client(base_url="http://127.0.0.1:9200", timeout=30) as client:
        conn.execute("SET random_page_cost = 1.1")
        conn.execute("SET effective_io_concurrency = 200")
        conn.execute("SET enable_mergejoin = off")
        conn.execute("SET statement_timeout = '60s'")
        while not stop_event.is_set():
            _attendi(stop_event)
            righe = _batch_ids(conn, da, fine, cursor)
            if not righe:
                break
            cursor = _uuid(righe[-1]["id"])
            attivi = [str(_uuid(r["id"])) for r in righe if r["expired_at"] is None]
            documenti = opensearch_sync._documenti(opensearch_sync._righe(ids=attivi)) if attivi else {}
            azioni = []
            for r in righe:
                job_id = str(_uuid(r["id"]))
                if r["expired_at"] is not None or job_id not in documenti:
                    azioni.append(("delete", {"id": job_id}))
                else:
                    azioni.append(("index", documenti[job_id]))
            errori = _azioni(client, azioni, stop_event)
            stato["cursor"] = str(cursor)
            stato["upsert"] += sum(op == "index" for op, _ in azioni)
            stato["delete"] += sum(op == "delete" for op, _ in azioni)
            stato["errors"] += errori
            stato["duration_s"] = round(time.monotonic() - inizio, 1)
            _save(stato)
            velocita = len(righe) / max(time.monotonic() - inizio, 0.001)
            print(f"catchup indexed={stato['upsert']} delete={stato['delete']} "
                  f"errors={stato['errors']} cursor={cursor} rate={velocita:.1f}/s", flush=True)
    stato["status"] = "complete"
    stato["duration_s"] = round(time.monotonic() - inizio, 1)
    _save(stato)
    return stato


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--da", required=True)
    ap.add_argument("--fine")
    ap.add_argument("--ultimi-secondi", type=int)
    args = ap.parse_args()
    da = datetime.fromisoformat(args.da)
    if args.ultimi_secondi:
        da = datetime.fromisoformat(args.da) - timedelta(seconds=args.ultimi_secondi)
    fine = datetime.fromisoformat(args.fine) if args.fine else datetime.now(UTC)
    run(da, fine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
