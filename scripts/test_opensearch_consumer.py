from __future__ import annotations

import json
import uuid

import opensearch_sync
from nivult.api_clienti import vivo

V2_FIELDS = {"source_type", "company", "company_slug", "company_domain", "city", "state",
             "employment_type", "department", "skills", "has_salary", "salary_min",
             "salary_max", "salary_currency"}


def test_documento_consumer_uuid_e_campi_v2(monkeypatch):
    job_id = uuid.UUID("12345678-1234-5678-9abc-def012345678")
    righe = [{
        "id": job_id, "title": "Systems Engineer", "platform_id": "workday", "slug": "acme",
        "company": "Acme", "source_type": "direct", "company_domain": "acme.com",
        "url": "https://acme.example/job/1", "country": "US", "city": "Boston",
        "location": "Boston", "lang": "en", "seniority": "senior", "remote": "remote",
        "skills": ["MBSE"], "salary_min": 100000, "salary_max": 150000,
        "salary_currency": "USD", "posted_at": None, "first_seen": None, "last_seen": None,
        "description": "MBSE " * 100, "category": "Technology", "employment_type": "full_time",
        "languages_required": [], "external_id": "1", "department": "Engineering",
        "orario": None, "durata": None, "salary_period": None, "salary_da": None,
        "salary_text": None,
        "posted_at_estimated": False, "tecnologie": [], "sintesi": None, "sintesi_da": None,
        "management_level": None, "is_decision_maker": None, "shift_schedule": None,
        "work_hours": None, "is_urgently_hiring": None, "benefits": [], "valid_through": None,
        "state": "MA", "postal_code": None, "latitude": None, "longitude": None,
        "geo_da": None, "applicants_count": None, "is_easy_apply": None,
        "content_updated_at": None,
    }]
    monkeypatch.setattr(vivo, "_fonti_per_righe", lambda ids: {})
    docs = opensearch_sync._documenti(righe)
    doc = docs[str(job_id)]
    assert doc["id"] == str(job_id)
    assert V2_FIELDS <= set(doc)
    assert doc["source_type"] == "direct"
    assert doc["has_salary"] is True
    assert doc["salary_min"] == 100000.0


def test_bulk_consumer_serializza_uuid_e_ignora_delete_404(monkeypatch):
    class Client:
        def __init__(self):
            self.body = ""

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post(self, path, content, headers):
            self.body = content.decode()
            return type("Response", (), {
                "raise_for_status": lambda self: None,
                "json": lambda self: {"errors": False, "items": []},
            })()

    client = Client()
    monkeypatch.setattr(opensearch_sync, "client", lambda: client)
    job_id = uuid.UUID("12345678-1234-5678-9abc-def012345678")
    opensearch_sync._bulk([("delete", {"id": str(job_id)})])
    payload = json.loads(client.body.splitlines()[0])
    assert payload["delete"]["_id"] == str(job_id)
