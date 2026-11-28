import collections
import gzip
import json

import pytest

from nivult.ats.export_quality import (
    InvariantError,
    aggiorna_indice,
    azienda_da_indice,
    indice_nuovo,
    pubblica_generazione,
    stratifica,
    valida_job_sample,
    verifica_export,
)
from nivult.ats.testo import pulito


def job(i, ats="greenhouse", slug="acme", company="Acme", country="IT", tech=None):
    return {
        "id": str(i), "ats": ats, "company_slug": slug, "company": company,
        "country": country, "title": "Engineer", "url": f"https://example.com/{i}",
        "posted_at": "2026-11-20T00:00:00+00:00", "first_seen": "2026-11-20T00:00:00+00:00",
        "last_seen": "2026-11-28T00:00:00+00:00", "description": "A real job description",
        "language": "en", "skills": ["python"], "technologies": tech or ["Python"],
    }


def company_from_jobs(jobs):
    indice = indice_nuovo()
    for r in jobs:
        aggiorna_indice(indice, r)
    return [azienda_da_indice(a) for a in indice.values()]


def write_jsonl(path, rows):
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")


def test_canonical_index_enforces_zero_orphans_missing_ghost_and_no_top25(tmp_path):
    jobs = [job(1, ats="francetravail", slug="jobboerse", company="Real Employer",
                tech=[f"Tech{i}" for i in range(30)]),
            job(2, ats="greenhouse", slug="acme", company="Acme", country="US", tech=["Go"])]
    companies = company_from_jobs(jobs)
    write_jsonl(tmp_path / "jobs.jsonl.gz", jobs)
    write_jsonl(tmp_path / "companies.jsonl.gz", companies)
    assert len(companies[0]["technologies"]) == 30
    assert verifica_export(str(tmp_path / "jobs.jsonl.gz"),
                           str(tmp_path / "companies.jsonl.gz")) == {}


def test_orphans_missing_and_ghost_pairs_fail(tmp_path):
    jobs = [job(1), job(2, slug="other", company="Other")]
    companies = company_from_jobs(jobs)
    write_jsonl(tmp_path / "jobs.jsonl.gz", jobs)
    write_jsonl(tmp_path / "companies.jsonl.gz", companies[:1])
    with pytest.raises(InvariantError) as exc:
        verifica_export(str(tmp_path / "jobs.jsonl.gz"),
                        str(tmp_path / "companies.jsonl.gz"))
    assert exc.value.args[0]["orphan_jobs"] == 1
    assert exc.value.args[0]["missing_tech_pairs"] == 1
    ghost = dict(companies[0])
    ghost["technologies"] = [*ghost["technologies"],
                             {"technology": "Ghost", "active_jobs": 1, "jobs": 1}]
    write_jsonl(tmp_path / "companies.jsonl.gz", [ghost, companies[1]])
    with pytest.raises(InvariantError) as exc:
        verifica_export(str(tmp_path / "jobs.jsonl.gz"),
                        str(tmp_path / "companies.jsonl.gz"))
    assert exc.value.args[0]["ghost_tech_pairs"] == 1
    assert exc.value.args[0]["tech_count_mismatch"] == 1


def test_stratified_sample_is_deterministic_and_respects_caps():
    rows = []
    countries = ["IT", "FR", "DE", "GB", "US", "IN", "BR", "JP"]
    sources = ["mycareersfuture", "greenhouse", "workday", "lever", "icims", "ashby",
               "smartrecruiters", "teamtailor"]
    for i in range(900):
        ats = sources[i % len(sources)]
        country = countries[(i + i // len(sources)) % len(countries)]
        rows.append(job(i, ats=ats, slug=f"{ats}-{i}", company=f"Company {i}", country=country,
                        tech=["Python", "Go"] if i % 2 else ["SAP"]))
    first = stratifica(iter(rows), 500)
    second = stratifica(iter(rows), 500)
    assert first == second
    assert len(first) == 500
    assert len({(r["ats"], r["company_slug"]) for r in first}) == 500
    assert max(collections.Counter(r["ats"] for r in first).values()) <= 75
    assert max(collections.Counter(r["country"] for r in first).values()) <= 100
    assert {"IT", "GB", "US", "IN"}.issubset({r["country"] for r in first})
    assert collections.Counter(r["ats"] for r in first)["mycareersfuture"] <= 75


def test_sample_rejects_pii_html_and_placeholder():
    bad = job(1)
    bad["description"] = "Email first.last@example.com"
    with pytest.raises(InvariantError):
        valida_job_sample(bad)
    bad = job(1, company="Confidential")
    with pytest.raises(InvariantError):
        valida_job_sample(bad)
    bad = job(1)
    bad["title"] = "<p>Engineer &amp;</p>"
    with pytest.raises(InvariantError):
        valida_job_sample(bad)


def test_text_cleaner_redacts_contacts_not_numeric_business_data():
    text = pulito("<p>Contact first.last@example.com +1 (415) 555-2671. "
                  "Salary €60,000–€90,000, REQ-2026-123456, 2026-11-28.</p>")
    assert "first.last@example.com" not in text
    assert "555-2671" not in text
    assert "[redacted]" in text
    assert "€60,000–€90,000" in text
    assert "REQ-2026-123456" in text
    assert "2026-11-28" in text


def test_publication_is_atomic_on_validation_failure(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    old_manifest = {"state": "ready", "rows": 999}
    (root / "manifest-ultimo.json").write_text(json.dumps(old_manifest))
    jobs = [job(1)]
    companies = company_from_jobs(jobs)
    staging = tmp_path / "staging"
    staging.mkdir()
    write_jsonl(staging / "jobs-2026-11-28.jsonl.gz", jobs)
    write_jsonl(staging / "companies-2026-11-28.jsonl.gz", companies)
    manifest = {"date": "2026-11-28", "rows": 1, "companies_rows": 1}
    ready = pubblica_generazione(str(staging), str(root), manifest,
                                 "jobs-2026-11-28.jsonl.gz", "companies-2026-11-28.jsonl.gz",
                                 "manifest-ultimo.json")
    assert ready["state"] == "ready"
    assert ready["invariants"] == {}
    assert ready["files"]["jobs"] == "jobs-2026-11-28.jsonl.gz"
    assert json.loads((root / "manifest-ultimo.json").read_text())["rows"] == 1
    bad_staging = tmp_path / "bad-staging"
    bad_staging.mkdir()
    write_jsonl(bad_staging / "jobs-2026-11-29.jsonl.gz", jobs)
    write_jsonl(bad_staging / "companies-2026-11-29.jsonl.gz", [])
    with pytest.raises(InvariantError):
        pubblica_generazione(str(bad_staging), str(root), manifest,
                             "jobs-2026-11-29.jsonl.gz", "companies-2026-11-29.jsonl.gz",
                             "manifest-ultimo.json")
    assert json.loads((root / "manifest-ultimo.json").read_text())["rows"] == 1


def test_latest_file_lookup_never_mixes_full_and_sample(tmp_path):
    from nivult.ats.esporta import _ultimo_file
    for name in ("offerte-attive-2026-11-28.jsonl.gz",
                 "offerte-attive-campione-2026-11-29.jsonl.gz",
                 "offerte-attive-2026-11-27.jsonl.gz"):
        (tmp_path / name).touch()
    assert _ultimo_file(str(tmp_path), "offerte-attive").endswith("2026-11-28.jsonl.gz")
    assert _ultimo_file(str(tmp_path), "offerte-attive-campione").endswith("2026-11-29.jsonl.gz")


def test_publication_rolls_back_files_and_links_if_manifest_commit_fails(tmp_path, monkeypatch):
    import os
    root = tmp_path / "root"
    root.mkdir()
    old_jobs = root / "offerte-attive-2026-11-01-old.jsonl.gz"
    old_jobs.write_text("old")
    (root / "manifest-ultimo.json").write_text('{"state":"ready","rows":999}')
    (root / "offerte-attive-ultimo.jsonl.gz").symlink_to(old_jobs.name)
    jobs = [job(1)]
    staging = tmp_path / "staging"
    staging.mkdir()
    write_jsonl(staging / "jobs-2026-11-28.jsonl.gz", jobs)
    write_jsonl(staging / "companies-2026-11-28.jsonl.gz", company_from_jobs(jobs))
    original_replace = os.replace

    def fail_manifest(src, dst):
        if str(dst).endswith("manifest-ultimo.json"):
            raise OSError("simulated manifest failure")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "replace", fail_manifest)
    with pytest.raises(OSError, match="simulated manifest failure"):
        pubblica_generazione(str(staging), str(root), {"rows": 1, "companies_rows": 1},
                             "jobs-2026-11-28.jsonl.gz", "companies-2026-11-28.jsonl.gz",
                             "manifest-ultimo.json")
    assert not (root / "jobs-2026-11-28.jsonl.gz").exists()
    assert not (root / "companies-2026-11-28.jsonl.gz").exists()
    assert json.loads((root / "manifest-ultimo.json").read_text())["rows"] == 999
    assert os.readlink(root / "offerte-attive-ultimo.jsonl.gz") == old_jobs.name


def test_sample_endpoint_uses_only_ready_manifest_generation(tmp_path, monkeypatch):
    from nivult.api_clienti import v1
    monkeypatch.setenv("EXPORT_DIR", str(tmp_path))
    jobs_name = "offerte-attive-campione-2026-11-28-abc123def456.jsonl.gz"
    companies_name = "aziende-segnali-campione-2026-11-28-abc123def456.jsonl.gz"
    (tmp_path / jobs_name).touch()
    (tmp_path / companies_name).touch()
    (tmp_path / "manifest-campione.json").write_text(json.dumps({
        "state": "ready", "files": {"jobs": jobs_name, "companies": companies_name}}))
    assert v1._campione_export("offerte").endswith("abc123def456.jsonl.gz")
    assert v1._campione_export("aziende").endswith("abc123def456.jsonl.gz")
    (tmp_path / "manifest-campione.json").write_text('{"state":"building"}')
    assert v1._campione_export("offerte") is None
    assert v1._campione_export("aziende") is None
