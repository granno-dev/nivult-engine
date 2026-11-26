from __future__ import annotations

import uuid
from contextlib import contextmanager

from nivult.api_clienti import vivo
from nivult.ats.titoli import da_url, pulisci


def test_numeric_title_recovers_real_job_slug_and_never_shows_number():
    url = "https://careers.halliburton.com/job/london/solutions-geoscientist-i/543/101346282304/"
    assert da_url(url) == "Solutions geoscientist i"
    assert pulisci("543", url) == "Solutions geoscientist i"
    assert pulisci("543") == "Job title unavailable"


def test_postal_and_requisition_suffixes_are_removed_only_when_unambiguous():
    assert pulisci("Research Triangle Park Country Controller NC 27709 3528") == (
        "Research Triangle Park Country Controller")
    assert pulisci("Calgary Représentant principal du service à la clientèle "
                   "Bilingue AB T2C 5P9") == (
        "Calgary Représentant principal du service à la clientèle Bilingue")
    assert pulisci("Software Engineer 2") == "Software Engineer 2"


def test_duckdb_rows_use_indexed_postgres_url_lookup(monkeypatch):
    job_id = str(uuid.uuid4())

    class Connection:
        def execute(self, sql, params):
            assert "FROM ats_jobs" in sql and "uuid[]" in sql
            assert params == {"ids": [job_id]}
            return self

        def fetchall(self):
            return [{"id": job_id, "url": "https://jobs.example/job/paris/data-engineer/543/"}]

    @contextmanager
    def connection():
        yield Connection()

    monkeypatch.setattr(vivo, "_conn", connection)
    rows = [{"id": job_id, "title": "543"},
            {"id": str(uuid.uuid4()), "title": "Research Triangle Park Controller NC 27709 3528"}]
    assert vivo.completa_titoli_portale(rows) == [
        {"id": job_id, "title": "Data engineer"},
        {"id": rows[1]["id"], "title": "Research Triangle Park Controller"},
    ]
