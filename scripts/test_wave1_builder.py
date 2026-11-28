import gzip
import json
import os
import resource

import duckdb
import pytest

from nivult.api_clienti.aggiorna import (
    _carica_nativa,
    _controlla_invarianti,
    _id_raw_mismatch,
    costruisci,
)
from nivult.ats.export_quality import aggiorna_indice, azienda_da_indice, indice_nuovo


def job(i, ats="greenhouse", slug="acme", company="Acme", country="IT", tech=None):
    return {
        "id": str(i), "ats": ats, "company_slug": slug, "company": company,
        "country": country, "title": "Engineer", "url": f"https://example.com/{i}",
        "posted_at": "2026-11-20T00:00:00+00:00", "first_seen": "2026-11-20T00:00:00+00:00",
        "last_seen": "2026-11-28T00:00:00+00:00", "description": "A real description",
        "language": "en", "skills": ["python"], "technologies": tech or ["Python"],
        "source_type": "direct", "has_salary": False,
    }


def write_jsonl(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def companies_from_jobs(jobs):
    indice = indice_nuovo()
    for r in jobs:
        aggiorna_indice(indice, r)
    return [azienda_da_indice(a) for a in indice.values()]


def write_generation(cartella, jobs, companies, date="2026-11-28"):
    jobs_name = f"offerte-attive-{date}.jsonl.gz"
    companies_name = f"aziende-segnali-{date}.jsonl.gz"
    write_jsonl(cartella / jobs_name, jobs)
    write_jsonl(cartella / companies_name, companies)
    manifest = {
        "state": "ready", "date": date, "rows": len(jobs), "companies_rows": len(companies),
        "coverage": {"description": 100.0}, "coverage_aziende": {"country": 100.0},
        "files": {"jobs": jobs_name, "companies": companies_name},
    }
    (cartella / "manifest-ultimo.json").write_text(json.dumps(manifest))
    return manifest


def test_builder_end_to_end_and_invariants(tmp_path):
    jobs = [job(1), job(2, ats="francetravail", slug="jobboerse", company="Real Employer",
                        country="FR", tech=["Go"])]
    write_generation(tmp_path, jobs, companies_from_jobs(jobs))
    db = tmp_path / "api.duckdb"
    meta = costruisci(str(tmp_path), db_path=str(db), attesa_minuti=0)
    assert meta["stato"] == "ok"
    assert meta["offerte"] == 2 and meta["aziende"] == 2
    con = duckdb.connect(str(db), read_only=True)
    try:
        assert _controlla_invarianti(con) == {
            "orphan_jobs": 0, "companies_without_jobs": 0, "missing_tech_pairs": 0,
            "ghost_tech_pairs": 0, "company_count_mismatch": 0, "tech_count_mismatch": 0,
        }
        raw = con.execute("SELECT raw FROM offerte WHERE id='1'").fetchone()[0]
        assert json.loads(raw)["title"] == "Engineer"
        assert not any(json.loads(con.execute(
            "SELECT valore FROM meta WHERE chiave='invarianti'").fetchone()[0]).values())
    finally:
        con.close()


def test_builder_preserves_previous_db_on_ghost_and_missing_file(tmp_path):
    jobs = [job(1)]
    write_generation(tmp_path, jobs, companies_from_jobs(jobs))
    db = tmp_path / "api.duckdb"
    costruisci(str(tmp_path), db_path=str(db), attesa_minuti=0)
    before = db.read_bytes()
    companies = companies_from_jobs(jobs)
    companies[0]["technologies"].append(
        {"technology": "Ghost", "active_jobs": 1, "jobs": 1})
    write_generation(tmp_path, jobs, companies)
    with pytest.raises(ValueError, match="ghost_tech_pairs"):
        costruisci(str(tmp_path), db_path=str(db), attesa_minuti=0)
    assert db.read_bytes() == before
    os.remove(tmp_path / "aziende-segnali-2026-11-28.jsonl.gz")
    with pytest.raises((FileNotFoundError, ValueError)):
        costruisci(str(tmp_path), db_path=str(db), attesa_minuti=0)
    assert db.read_bytes() == before


def test_native_load_is_staged_and_preserves_verbatim_raw(tmp_path):
    path = tmp_path / "jobs.jsonl.gz"
    jobs = [job(i) for i in range(1000)]
    write_jsonl(path, jobs)
    con = duckdb.connect(":memory:")
    con.execute("SET memory_limit='1GB'")
    con.execute("SET threads=1")
    con.execute("SET preserve_insertion_order=false")
    con.execute("CREATE TABLE offerte(id VARCHAR,title VARCHAR,ats VARCHAR,"
                "company_slug VARCHAR,company VARCHAR,country VARCHAR,city VARCHAR,"
                "language VARCHAR,seniority VARCHAR,remote VARCHAR,category VARCHAR,"
                "posted_at TIMESTAMP,technologies VARCHAR[],raw VARCHAR)")
    from nivult.api_clienti.aggiorna import _COLONNE_OFFERTE
    assert _carica_nativa(con, str(path), "offerte", _COLONNE_OFFERTE) == 1000
    raw = con.execute("SELECT raw FROM offerte WHERE id='999'").fetchone()[0]
    assert json.loads(raw)["id"] == "999"
    assert con.execute("SELECT count(*) FROM information_schema.tables "
                       "WHERE table_name='load_json'").fetchone()[0] == 0
    con.close()


def test_multichunk_id_raw_alignment_and_memory_ceiling(tmp_path):
    path = tmp_path / "jobs-multichunk.jsonl.gz"
    write_jsonl(path, [job(i) for i in range(20000)])
    con = duckdb.connect(":memory:")
    con.execute("SET memory_limit='1GB'")
    con.execute("SET threads=2")
    con.execute("SET preserve_insertion_order=false")
    con.execute("CREATE TABLE offerte(id VARCHAR,title VARCHAR,ats VARCHAR,"
                "company_slug VARCHAR,company VARCHAR,country VARCHAR,city VARCHAR,"
                "language VARCHAR,seniority VARCHAR,remote VARCHAR,category VARCHAR,"
                "posted_at TIMESTAMP,technologies VARCHAR[],raw VARCHAR)")
    from nivult.api_clienti.aggiorna import _COLONNE_OFFERTE
    assert _carica_nativa(con, str(path), "offerte", _COLONNE_OFFERTE) == 20000
    assert _id_raw_mismatch(con) == 0
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss < 1024**3
    con.execute("UPDATE offerte SET id='mismatch' WHERE id='19999'")
    assert _id_raw_mismatch(con) == 1
    con.close()
