from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["API_OPENSEARCH_JOBS"] = "1"

from nivult.api_clienti import opensearch, v1


def test_query_v2_e_legacy():
    query = opensearch._query({
        "source_type": "direct,aggregator", "company": "The Dixon Group",
        "company_slug": "acme,other", "company_domain": "https://WWW.dixonvalve.com/careers/",
        "city": "Boston,New York", "state": "MA", "employment_type": "full_time",
        "department": "Engineering", "skills": "MBSE,SysML", "salary_currency": "USD",
        "has_salary": True, "salary_min": 100000, "salary_max": 150000,
    })["bool"]
    assert {"terms": {"source_type": ["direct", "aggregator"]}} in query["filter"]
    assert {"term": {"company_domain": "dixonvalve.com"}} in query["filter"]
    assert {"terms": {"city": ["boston", "new york"]}} in query["filter"]
    assert {"term": {"has_salary": True}} in query["filter"]
    assert {"range": {"salary_min": {"gte": 100000.0}}} in query["filter"]
    assert {"range": {"salary_max": {"lte": 150000.0}}} in query["filter"]
    assert query["must"] == [{"match": {"company": {
        "query": "The Dixon Group", "operator": "and"}}}]
    legacy = opensearch._query({"country": "US", "dal": "2026-10-24T00:00:00Z", "q": "MBSE"})
    assert legacy["bool"]["filter"][0] == {"term": {"country": "us"}}
    assert legacy["bool"]["must"][0]["multi_match"]["operator"] == "and"


@pytest.mark.parametrize("params", [
    {"source_type": "unknown"}, {"salary_min": "-1"},
    {"salary_min": "200000", "salary_max": "100000"},
    {"salary_min": "abc"}, {"has_salary": "perhaps"},
])
def test_http_400_senza_opensearch_o_crediti(params):
    app = FastAPI()
    app.include_router(v1.router)
    client = TestClient(app)
    chiamate = {"os": 0, "metering": 0}

    def offerte(*args):
        chiamate["os"] += 1
        return [], None

    def addebito(*args, **kwargs):
        chiamate["metering"] += 1

    originali = v1.chiavi.verifica, v1.chiavi.addebita_chiave, v1.opensearch.offerte
    v1.chiavi.verifica = lambda chiave: {
        "id": "test", "user_id": "test", "monthly_tier": 10000,
        "crediti_mensili": 10000, "usati_mese": 0, "mese_uso": None}
    v1.chiavi.addebita_chiave = addebito
    v1.opensearch.offerte = offerte
    v1._rate_limiter.clear()
    try:
        r = client.get("/v1/jobs", params=params, headers={"X-Api-Key": "test"})
        assert r.status_code == 400
        assert chiamate == {"os": 0, "metering": 0}
    finally:
        v1.chiavi.verifica, v1.chiavi.addebita_chiave, v1.opensearch.offerte = originali
        v1._rate_limiter.clear()


@pytest.mark.skipif(os.environ.get("CHECK_OPENSEARCH_LIVE") != "1",
                    reason="query live disattivate")
def test_live_v2_sola_lettura():
    legacy = {"country": "US", "dal": "2026-10-24T00:00:00Z", "q": "MBSE"}
    assert opensearch.conteggio(legacy) > 0
    righe, _ = opensearch.offerte({"source_type": "direct", "q_any": "MBSE,SysML"}, None, 10)
    assert righe and all(r["source_type"] == "direct" for r in righe)
    assert opensearch.conteggio({"company": "The Dixon Group"}) > 0
    assert opensearch.conteggio({"company_domain": "dixonvalve.com"}) > 0
    assert opensearch.conteggio({"has_salary": True, "salary_min": 100000,
                                 "salary_currency": "usd"}) > 0
    assert opensearch.conteggio({"city": "Boston"}) > 0
    assert opensearch.conteggio({"employment_type": "full_time"}) > 0
