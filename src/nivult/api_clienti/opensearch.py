from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
from contextlib import contextmanager

import httpx
import psycopg

URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")
INDEX = os.environ.get("OPENSEARCH_INDEX", "nivult_jobs_active")
_client: httpx.Client | None = None


def configura_client(client: httpx.Client | None) -> None:
    global _client
    _client = client


@contextmanager
def _http_client():
    if _client is not None:
        yield _client
        return
    with httpx.Client(
            base_url=URL,
            timeout=httpx.Timeout(20.0, connect=2.0),
            limits=httpx.Limits(max_connections=16, max_keepalive_connections=16,
                                keepalive_expiry=30.0)) as client:
        yield client


def _norm_ts(v) -> str | None:
    if not v:
        return None
    d = dt.datetime.fromisoformat(str(v))
    if d.tzinfo:
        d = d.astimezone(dt.UTC).replace(tzinfo=None)
    return d.isoformat()


def _cursore(chiave: list) -> str:
    return base64.urlsafe_b64encode(json.dumps(chiave).encode()).decode()


def _leggi_cursore(cursore: str) -> list:
    try:
        chiave = json.loads(base64.urlsafe_b64decode(cursore.encode()))
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"unreadable cursor: {cursore!r}") from e
    if not isinstance(chiave, list):
        raise ValueError(f"unreadable cursor: {cursore!r}")
    return chiave


def attivo() -> bool:
    return os.environ.get("API_OPENSEARCH_JOBS", "0").lower() in ("1", "true", "yes")


def _query(filtri: dict) -> dict:
    bool_query = {"filter": []}
    for campo in ("country", "category", "ats", "seniority", "language", "remote"):
        if filtri.get(campo) is not None:
            valore = filtri[campo]
            if campo == "remote" and isinstance(valore, bool):
                valore = "remote" if valore else "onsite"
            bool_query["filter"].append({"term": {campo: str(valore).lower()}})
    if filtri.get("technology"):
        bool_query["filter"].append(
            {"term": {"technologies": str(filtri["technology"]).lower()}})
    if filtri.get("dal"):
        bool_query["filter"].append(
            {"range": {"posted_at": {"gte": _norm_ts(str(filtri["dal"]))}}})
    if filtri.get("q"):
        bool_query["must"] = [{"multi_match": {
            "query": str(filtri["q"]), "fields": ["title^2", "description"],
            "operator": "and", "type": "best_fields"}}]
    return {"bool": bool_query}


def _chiamata(c: httpx.Client, percorso: str, body: dict) -> dict:
    try:
        r = c.post(percorso, json=body)
        r.raise_for_status()
        return r.json()
    except httpx.TimeoutException as e:
        raise psycopg.errors.QueryCanceled("OpenSearch query timed out") from e
    except httpx.HTTPError as e:
        raise psycopg.OperationalError("OpenSearch temporarily unavailable") from e


def offerte(filtri: dict, cursore: str | None,
            limite: int) -> tuple[list[dict], str | None]:
    limite = max(1, min(int(limite or 100), 500))
    body = {"query": _query(filtri), "track_total_hits": False,
            "size": limite + 1, "_source": True,
            "sort": [{"posted_at": {"order": "desc", "missing": "_last"}},
                     {"id": {"order": "desc", "missing": "_last"}}]}
    if cursore:
        ts, pid = _leggi_cursore(cursore)
        body["search_after"] = [_norm_ts(ts) if ts else None, pid]
    with _http_client() as c:
        esito = _chiamata(c, f"/{INDEX}/_search", body)
    hits = esito["hits"]["hits"]
    prossimo = None
    if len(hits) > limite:
        doc = hits[limite - 1]["_source"]
        prossimo = _cursore([doc.get("posted_at"), str(doc["id"])])
        hits = hits[:limite]
    return [h["_source"] for h in hits], prossimo


def conteggio(filtri: dict) -> int:
    with _http_client() as c:
        esito = _chiamata(c, f"/{INDEX}/_count", {"query": _query(filtri)})
    return int(esito["count"])
