"""Benchmark shadow: nessun cutover, nessuna modifica al traffico live."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import psycopg

from nivult.api_clienti import chiavi, dati, vivo
from opensearch_sync import _CANONICO, cerca_jobs, conteggio_jobs


def _ms(funzione):
    inizio = time.perf_counter()
    risultato = funzione()
    return round((time.perf_counter() - inizio) * 1000), risultato


def _canon(doc: dict) -> str:
    doc = dict(doc)
    doc.pop("last_seen", None)
    for campo in list(doc):
        if campo.startswith("salary_estimate_"):
            doc.pop(campo, None)
    if "job_sources" in doc:
        doc["job_sources"] = [s for s in (doc["job_sources"] or [])
                              if any(v is not None for v in s.values())]
    return json.dumps(doc, sort_keys=True, default=vivo._iso)


def _parita(righe: list[dict]) -> int:
    verificati = 0
    for doc in righe[:3]:
        atteso = vivo.rivela_offerta(doc["id"])
        if atteso is not None and _canon(atteso) == _canon(doc):
            verificati += 1
    return verificati


def _stampa(nome: str, filtri: dict, profilo_dir: Path | None = None,
            pagine: int = 1) -> None:
    cursore = None
    visti = set()
    for pagina in range(1, pagine + 1):
        ms, esito = _ms(lambda: cerca_jobs(filtri, cursore=cursore, limite=25,
                                           profilo=bool(profilo_dir)))
        parita = _parita(esito["data"])
        nuovi = [d["id"] for d in esito["data"] if d["id"] not in visti]
        duplicati = len(esito["data"]) - len(nuovi)
        visti.update(nuovi)
        row = {"name": nome, "page": pagina, "status": 200, "ms": ms,
               "count": esito["count_page"], "parity_docs": parita,
               "duplicates": duplicati}
        print(json.dumps(row), flush=True)
        if profilo_dir and esito.get("profile"):
            (profilo_dir / f"{nome.replace('/', '_')}.json").write_text(
                json.dumps(esito["profile"], indent=2))
        cursore = esito["next_cursor"]
        if not cursore:
            break


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile-dir", default="")
    ap.add_argument("--changes", action="store_true")
    ap.add_argument("--exports", action="store_true")
    args = ap.parse_args()
    profile_dir = Path(args.profile_dir) if args.profile_dir else None
    if profile_dir:
        profile_dir.mkdir(parents=True, exist_ok=True)

    for paese in ("US", "DE", "GB", "FR", "IT", "CA", "IN"):
        _stampa(f"country_{paese}_Teamcenter",
                {"country": paese, "q": "Teamcenter", "dal": "2026-10-22"})
    _stampa("global_Teamcenter", {"q": "Teamcenter", "dal": "2026-10-22"})
    for q in ("Engineer", "Python", "SAP", "Sales"):
        _stampa(f"generic_{q}", {"q": q})
    _stampa("technology_Teamcenter", {"technology": "Teamcenter"})
    _stampa("technology_US_Teamcenter_q", {"country": "US", "technology": "Teamcenter",
                                           "q": "Teamcenter"})
    _stampa("pagination_Python", {"q": "Python"}, pagine=3)
    _stampa("null_posted", {"_null_posted": True}, pagine=3)
    if profile_dir:
        _stampa("profile_Engineer", {"q": "Engineer"}, profilo_dir=profile_dir)
        _stampa("profile_Python", {"q": "Python"}, profilo_dir=profile_dir)

    if args.exports:
        with psycopg.connect(vivo._DSN) as con:
            con.execute("SET statement_timeout = 0")
            canonici_pg = con.execute(
                f"SELECT count(*) FROM ats_jobs j WHERE {_CANONICO}").fetchone()[0]
        os_ms, os_count = _ms(lambda: conteggio_jobs({}))
        print(json.dumps({"name": "canonical_active", "postgres_count": canonici_pg,
                          "opensearch_count": os_count, "opensearch_ms": os_ms}), flush=True)
        for nome, filtri in (("global", {}),
                             ("US_SAP_date", {"country": "US", "technology": "SAP",
                                              "dal": "2026-10-22"})):
            os_ms, os_count = _ms(lambda: conteggio_jobs(filtri))
            duck_ms, duck_count = _ms(lambda: dati.stima_export(filtri))
            print(json.dumps({"name": f"exports_{nome}", "opensearch_ms": os_ms,
                              "opensearch_count": os_count, "duckdb_ms": duck_ms,
                              "duckdb_count": duck_count}), flush=True)

    if args.changes:
        key, key_id = chiavi.nuova("benchmark shadow changes", 500000)
        try:
            since = (datetime.now(UTC) - timedelta(days=7)).isoformat()
            with httpx.Client(base_url="https://api.nivult.com", timeout=60,
                              headers={"X-Api-Key": key, "User-Agent": "Mozilla/5.0"}) as c:
                ms, r = _ms(lambda: c.get("/v1/changes", params={"since": since, "limit": 25}))
                print(json.dumps({"name": "changes_7d", "status": r.status_code, "ms": ms,
                                  "count": r.json().get("count_page")}), flush=True)
        finally:
            chiavi._revoca(key_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
