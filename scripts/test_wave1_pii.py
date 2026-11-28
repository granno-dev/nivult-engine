from copy import deepcopy

import pytest

from nivult.ats.pii import contains_pii, redact_emails, redact_phones, redact_pii, sanitize_record


@pytest.mark.parametrize(
    "phone",
    [
        "+39 333 123 4567",
        "+44 20 7946 0958",
        "+1 555 123 4567",
        "+1 (415) 555-2671",
        "+39-333-123-4567",
        "0039 333 123 4567",
        "(555) 123-4567",
    ],
)
def test_supported_international_and_nanp_phones(phone):
    text = f"Call {phone}."

    assert redact_phones(text) == "Call [redacted]."
    assert redact_pii(text) == "Call [redacted]."
    assert contains_pii(text) is True


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Phone: 02 1234 5678", "Phone: [redacted]"),
        ("tel. 02 1234 5678", "tel. [redacted]"),
        ("Mobile - 02 1234 5678", "Mobile - [redacted]"),
        ("WhatsApp: 02 1234 5678", "WhatsApp: [redacted]"),
        ("Telefono: 02 1234 5678", "Telefono: [redacted]"),
        ("Telefon: 02 1234 5678", "Telefon: [redacted]"),
        ("Cellulare: 333 123 4567", "Cellulare: [redacted]"),
    ],
)
def test_national_phone_requires_and_preserves_explicit_label(text, expected):
    assert redact_phones(text) == expected
    assert contains_pii(text) is True


def test_all_emails_use_the_same_marker():
    text = "Contact first.last+hr@example.com and HR@Example.CO.UK."
    expected = "Contact [redacted] and [redacted]."

    assert redact_emails(text) == expected
    assert redact_pii(text) == expected
    assert contains_pii(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "€60,000–€90,000",
        "60000-90000 EUR",
        "2026-11-28",
        "28/11/2026",
        "10115",
        "SW1A 1AA",
        "REQ-2026-123456",
        "R-0001234567",
        "Job ID 1234567890",
    ],
)
def test_salaries_dates_postal_codes_and_job_ids_are_unchanged(text):
    assert redact_phones(text) == text
    assert redact_pii(text) == text
    assert contains_pii(text) is False


@pytest.mark.parametrize("digits", ["123456789", "1234567890", "123456789012345"])
def test_bare_nine_to_fifteen_digit_runs_are_unchanged(digits):
    assert redact_phones(digits) == digits
    assert contains_pii(digits) is False


@pytest.mark.parametrize(
    "text",
    [
        "+39\n333 123 4567",
        "+12345678",
        "+1234567890123456",
        "A+39 333 123 4567",
        "+39 333 123 4567B",
        "02 1234 5678",
        "R-000123456789",
    ],
)
def test_phone_boundaries_digit_limits_and_separator_rules(text):
    assert redact_phones(text) == text
    assert contains_pii(text) is False


def test_sanitizer_redacts_only_free_text_and_removes_contact_keys_recursively():
    record = {
        "title": "Engineer — jobs@example.com",
        "company": "Example — hr@example.com",
        "description": "Call +39 333 123 4567",
        "summary": "Call (555) 123-4567",
        "about": "Email about@example.com",
        "body": "Phone: 02 1234 5678",
        "salary_text": "€60,000–€90,000",
        "url": "https://example.com/jobs?email=jobs@example.com",
        "external_id": "R-0001234567",
        "posted_at": "2026-11-28",
        "postal_code": "SW1A 1AA",
        "salary_min": 60000,
        "salary_max": 90000,
        "notes": "Keep admin@example.com",
        "contact_email": "recruiter@example.com",
        "recruiter": {"name": "Example Recruiter"},
        "nested": {
            "contact_email": "nested@example.com",
            "recruiter": "Nested Recruiter",
            "description": "WhatsApp: 02 1234 5678",
            "metadata": "Keep nested@example.com",
        },
        "items": [{"title": "Contact item@example.com", "external_id": "REQ-2026-123456"}],
    }
    original = deepcopy(record)

    sanitized = sanitize_record(record)

    assert sanitized["title"] == "Engineer — [redacted]"
    assert sanitized["company"] == "Example — [redacted]"
    assert sanitized["description"] == "Call [redacted]"
    assert sanitized["summary"] == "Call [redacted]"
    assert sanitized["about"] == "Email [redacted]"
    assert sanitized["body"] == "Phone: [redacted]"
    assert sanitized["salary_text"] == "€60,000–€90,000"
    assert sanitized["url"] == record["url"]
    assert sanitized["external_id"] == "R-0001234567"
    assert sanitized["posted_at"] == "2026-11-28"
    assert sanitized["postal_code"] == "SW1A 1AA"
    assert sanitized["salary_min"] == 60000
    assert sanitized["salary_max"] == 90000
    assert sanitized["notes"] == "Keep admin@example.com"
    assert "contact_email" not in sanitized
    assert "recruiter" not in sanitized
    assert "contact_email" not in sanitized["nested"]
    assert "recruiter" not in sanitized["nested"]
    assert sanitized["nested"]["description"] == "WhatsApp: [redacted]"
    assert sanitized["nested"]["metadata"] == "Keep nested@example.com"
    assert sanitized["items"] == [{"title": "Contact [redacted]", "external_id": "REQ-2026-123456"}]
    assert record == original
    assert contains_pii(sanitized["notes"]) is True
