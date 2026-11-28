from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import os
import re
from contextlib import contextmanager

import httpx
import psycopg

from nivult.ats.pii import sanitize_record

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
        raise TypeError(f"unreadable cursor: {cursore!r}")
    return chiave


def attivo() -> bool:
    return os.environ.get("API_OPENSEARCH_JOBS", "0").lower() in ("1", "true", "yes")


def _keyword(campo: str, valore) -> dict:
    valori = [v.strip().lower() for v in str(valore).split(",") if v.strip()]
    if len(valori) == 1:
        return {"term": {campo: valori[0]}}
    return {"terms": {campo: valori}}


SOURCE_TYPES = frozenset({"direct", "aggregator", "staffing", "public_board"})


def _testo(keyword: str) -> dict:
    return {"multi_match": {
        "query": keyword, "fields": ["title^2", "description"],
        "operator": "and", "type": "best_fields"}}


def _dominio(valore: str) -> str:
    v = re.sub(r"^https?://", "", str(valore).strip().lower())
    return v.removeprefix("www.").split("/", 1)[0]


def _query(filtri: dict) -> dict:
    bool_query = {"filter": []}
    for campo in ("country", "category", "ats", "seniority", "language", "remote"):
        if filtri.get(campo) is not None:
            valore = filtri[campo]
            if campo == "remote" and isinstance(valore, bool):
                valore = "remote" if valore else "onsite"
            bool_query["filter"].append(_keyword(campo, valore))
    if filtri.get("technology"):
        bool_query["filter"].append(_keyword("technologies", filtri["technology"]))
    for campo in ("source_type", "company_slug", "city", "state", "employment_type",
                  "department", "skills", "salary_currency"):
        if filtri.get(campo) is not None:
            bool_query["filter"].append(_keyword(campo, filtri[campo]))
    if filtri.get("company_domain") is not None:
        valori = [_dominio(v) for v in str(filtri["company_domain"]).split(",")
                  if _dominio(v)]
        if valori:
            bool_query["filter"].append(_keyword("company_domain", ",".join(valori)))
    if filtri.get("company"):
        bool_query.setdefault("must", []).append({"match": {"company": {
            "query": str(filtri["company"]), "operator": "and"}}})
    if filtri.get("has_salary") is not None:
        bool_query["filter"].append({"term": {"has_salary": bool(filtri["has_salary"])}})
    if filtri.get("salary_min") is not None:
        bool_query["filter"].append(
            {"range": {"salary_min": {"gte": float(filtri["salary_min"])}}})
    if filtri.get("salary_max") is not None:
        bool_query["filter"].append(
            {"range": {"salary_max": {"lte": float(filtri["salary_max"])}}})
    if filtri.get("exclude_ats"):
        bool_query["must_not"] = [_keyword("ats", filtri["exclude_ats"])]
    if filtri.get("dal"):
        bool_query["filter"].append(
            {"range": {"posted_at": {"gte": _norm_ts(str(filtri["dal"]))}}})
    if filtri.get("posted_before"):
        bool_query["filter"].append(
            {"range": {"posted_at": {"lt": _norm_ts(str(filtri["posted_before"]))}}})
    if filtri.get("q"):
        bool_query.setdefault("must", []).append(_testo(str(filtri["q"])))
    if filtri.get("title"):
        bool_query.setdefault("must", []).append({"match": {"title": {
            "query": str(filtri["title"]), "operator": "and"}}})
    if filtri.get("q_any"):
        keywords = [v.strip() for v in str(filtri["q_any"]).split(",") if v.strip()]
        if len(keywords) > 15:
            raise ValueError("q_any accepts at most 15 comma-separated keywords")
        if keywords:
            bool_query["should"] = [_testo(keyword) for keyword in keywords]
            bool_query["minimum_should_match"] = 1
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
    return [sanitize_record(h["_source"]) for h in hits], prossimo


def conteggio(filtri: dict) -> int:
    with _http_client() as c:
        esito = _chiamata(c, f"/{INDEX}/_count", {"query": _query(filtri)})
    return int(esito["count"])
