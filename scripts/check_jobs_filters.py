#!/usr/bin/env python3
"""Verifica i filtri /v1/jobs; le query live sono esclusivamente di lettura."""
from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["API_OPENSEARCH_JOBS"] = "1"

from fastapi import FastAPI
from fastapi.testclient import TestClient

from nivult.api_clienti import opensearch, v1

PASSED = []
FAILED = []


def check(label, got, expected=True):
    if got == expected:
        PASSED.append(label)
        print(f"  ok    {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label}: {got!r} != {expected!r}")


def test_query() -> None:
    check("country singolo conserva term", opensearch._query({"country": " US "}),
          {"bool": {"filter": [{"term": {"country": "us"}}]}})
    check("country multiplo usa terms normalizzati",
          opensearch._query({"country": " US, de ,,FR"}),
          {"bool": {"filter": [{"terms": {"country": ["us", "de", "fr"]}}]}})
    for campo in ("category", "ats", "seniority", "language", "remote"):
        query = opensearch._query({campo: "Primo,SECONDO"})
        check(f"{campo} multiplo", query["bool"]["filter"],
              [{"terms": {campo: ["primo", "secondo"]}}])
    check("technology multiplo usa technologies",
          opensearch._query({"technology": "SAP, Teamcenter"})["bool"]["filter"],
          [{"terms": {"technologies": ["sap", "teamcenter"]}}])
    check("exclude_ats singolo usa must_not",
          opensearch._query({"exclude_ats": " JSONLD "})["bool"]["must_not"],
          [{"term": {"ats": "jsonld"}}])
    check("exclude_ats multiplo usa terms",
          opensearch._query({"exclude_ats": " JSONLD, indeed ,,"})["bool"]["must_not"],
          [{"terms": {"ats": ["jsonld", "indeed"]}}])
    conflitto = opensearch._query({"ats": "jsonld", "exclude_ats": "jsonld"})["bool"]
    check("stesso ATS incluso ed escluso resta contraddittorio",
          conflitto["filter"] == [{"term": {"ats": "jsonld"}}]
          and conflitto["must_not"] == [{"term": {"ats": "jsonld"}}])
    check("exclude_ats vuoto non aggiunge clausole",
          "must_not" in opensearch._query({"exclude_ats": ""})["bool"], False)
    check("exclude_ats di sole virgole esclude un insieme vuoto",
          opensearch._query({"exclude_ats": ",,"})["bool"]["must_not"],
          [{"terms": {"ats": []}}])
    check("remote booleano retrocompatibile",
          opensearch._query({"remote": True})["bool"]["filter"],
          [{"term": {"remote": "remote"}}])

    legacy = opensearch._query({"q": "MBSE SysML"})
    check("q resta AND su titolo e descrizione", legacy["bool"]["must"],
          [{"multi_match": {"query": "MBSE SysML",
                            "fields": ["title^2", "description"],
                            "operator": "and", "type": "best_fields"}}])
    check("title cerca soltanto nel titolo",
          opensearch._query({"title": "systems engineer"})["bool"]["must"],
          [{"match": {"title": {"query": "systems engineer", "operator": "and"}}}])

    intervallo = opensearch._query({
        "dal": "2026-10-24T00:00:00Z", "posted_before": "2026-11-01T00:00:00Z"})
    check("posted_after inclusivo", intervallo["bool"]["filter"][0],
          {"range": {"posted_at": {"gte": "2026-10-24T00:00:00"}}})
    check("posted_before esclusivo", intervallo["bool"]["filter"][1],
          {"range": {"posted_at": {"lt": "2026-11-01T00:00:00"}}})

    query_any = opensearch._query({"q_any": " MBSE, SysML ,,CATIA "})["bool"]
    check("q_any genera tre clausole should", len(query_any["should"]), 3)
    check("q_any richiede almeno una keyword", query_any["minimum_should_match"], 1)
    check("q_any cerca su titolo e descrizione",
          all(clausola["multi_match"]["fields"] == ["title^2", "description"]
              for clausola in query_any["should"]))
    combinata = opensearch._query({"q": "engineer", "q_any": "MBSE,SysML"})["bool"]
    check("q e q_any sono cumulabili",
          len(combinata["must"]) == 1 and len(combinata["should"]) == 2
          and combinata["minimum_should_match"] == 1)
    try:
        opensearch._query({"q_any": ",".join(f"k{i}" for i in range(16))})
    except ValueError as exc:
        check("oltre 15 keyword solleva ValueError", str(exc),
              "q_any accepts at most 15 comma-separated keywords")
    else:
        check("oltre 15 keyword solleva ValueError", "nessuna eccezione", "ValueError")


def test_http_400_senza_addebito() -> None:
    app = FastAPI()
    app.include_router(v1.router)
    client = TestClient(app)
    originale_verifica = v1.chiavi.verifica
    originale_addebito = v1.chiavi.addebita_chiave
    originale_offerte = v1.opensearch.offerte
    chiamate = {"rete": 0, "metering": 0}

    def finte_offerte(filtri, cursore, limite):
        opensearch._query(filtri)
        chiamate["rete"] += 1
        return [], None

    v1.chiavi.verifica = lambda chiave: {
        "id": "test-key", "user_id": "test-user", "monthly_tier": 10_000,
        "crediti_mensili": 10_000, "usati_mese": 0, "mese_uso": None,
    }
    v1.chiavi.addebita_chiave = lambda *args, **kwargs: chiamate.__setitem__(
        "metering", chiamate["metering"] + 1)
    v1.opensearch.offerte = finte_offerte
    try:
        r = client.get("/v1/jobs", params={
            "q_any": ",".join(f"k{i}" for i in range(16))},
            headers={"X-Api-Key": "test"})
        check("16 keyword restituiscono HTTP 400", r.status_code, 400)
        check("errore 400 esplicita il limite", r.json()["detail"],
              "q_any accepts at most 15 comma-separated keywords")
        check("16 keyword non chiamano OpenSearch", chiamate["rete"], 0)
        check("16 keyword non addebitano crediti", chiamate["metering"], 0)
    finally:
        v1.chiavi.verifica = originale_verifica
        v1.chiavi.addebita_chiave = originale_addebito
        v1.opensearch.offerte = originale_offerte


def _timestamp(valore: str) -> dt.datetime:
    return dt.datetime.fromisoformat(valore)


def test_live() -> None:
    casi = [
        ("legacy", {"country": "US", "dal": "2026-10-24T00:00:00Z", "q": "MBSE"}),
        ("title", {"title": "SAP"}),
        ("posted_before", {"posted_before": "2026-11-01T00:00:00Z"}),
        ("country multiplo", {"country": "US,DE"}),
        ("ats multiplo", {"ats": "workday,greenhouse"}),
        ("seniority multiplo", {"seniority": "mid,senior"}),
        ("category multipla", {"category": "Human Resources,Sales"}),
        ("remote multiplo", {"remote": "remote,onsite"}),
        ("language multipla", {"language": "en,it"}),
        ("technology multipla", {"technology": "SAP,Teamcenter"}),
        ("q_any", {"q_any": "MBSE,SysML"}),
        ("exclude_ats", {"country": "US", "exclude_ats": "jsonld,indeed"}),
    ]
    for label, filtri in casi:
        try:
            totale = opensearch.conteggio(filtri)
            check(f"live {label}: match presenti", totale > 0, True)
            print(f"        count={totale}")
        except Exception as exc:  # noqa: BLE001
            FAILED.append(f"live {label}: {exc!r}")
            print(f"  FAIL  live {label}: {exc!r}")

    try:
        righe, _ = opensearch.offerte({"country": "US,DE"}, None, 25)
        check("live country: pagina piena", len(righe), 25)
        check("live country: soli paesi richiesti",
              {str(r["country"]).lower() for r in righe} <= {"us", "de"})
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"live pagina country: {exc!r}")

    try:
        righe, _ = opensearch.offerte(
            {"dal": "2026-10-01T00:00:00Z", "posted_before": "2026-11-01T00:00:00Z"},
            None, 25)
        check("live intervallo: date coerenti",
              all(dt.datetime(2026, 10, 1, tzinfo=dt.UTC) <= _timestamp(r["posted_at"])
                  < dt.datetime(2026, 11, 1, tzinfo=dt.UTC) for r in righe))
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"live intervallo: {exc!r}")

    try:
        righe, _ = opensearch.offerte({"title": "SAP"}, None, 25)
        check("live title: keyword nel solo titolo",
              bool(righe) and all("sap" in r["title"].lower() for r in righe))
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"live title: {exc!r}")

    try:
        righe, _ = opensearch.offerte({"q_any": "MBSE,SysML"}, None, 25)
        check("live q_any: almeno una keyword per documento",
              bool(righe) and all(any(k in f"{r['title']} {r['description']}".lower()
                                      for k in ("mbse", "sysml")) for r in righe))
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"live q_any: {exc!r}")

    try:
        righe, _ = opensearch.offerte(
            {"country": "US", "q_any": "MBSE,SysML", "exclude_ats": "jsonld"}, None, 10)
        check("live exclude_ats: pagina da 10 senza jsonld",
              len(righe) == 10 and all(r["ats"] != "jsonld" for r in righe))
        check("live ATS sia incluso sia escluso: zero risultati",
              opensearch.conteggio({"ats": "jsonld", "exclude_ats": "jsonld"}), 0)
    except Exception as exc:  # noqa: BLE001
        FAILED.append(f"live exclude_ats: {exc!r}")


def main() -> int:
    test_query()
    test_http_400_senza_addebito()
    if os.environ.get("CHECK_OPENSEARCH_LIVE") == "1":
        test_live()
    print(f"{len(PASSED)} controlli passati, {len(FAILED)} falliti")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
