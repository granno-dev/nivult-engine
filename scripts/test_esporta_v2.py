import json
from datetime import UTC, datetime

from nivult.ats.esporta import _riga, _riga_attiva


def _row(*, salary_min=None, salary_max=None, site_domain="Example.COM"):
    row = [None] * 52
    values = {
        0: "6c2c38de-7bb4-4f1e-b72b-92df9a0d06f1",
        1: "Data Engineer",
        2: "greenhouse",
        3: "example",
        4: "Example",
        5: "https://example.com/jobs/1",
        6: "US",
        7: "New York",
        8: "New York",
        9: "en",
        10: "mid",
        11: "hybrid",
        12: ["Python"],
        13: salary_min,
        14: salary_max,
        15: "USD" if salary_min is not None or salary_max is not None else None,
        16: datetime(2026, 11, 25, tzinfo=UTC),
        17: datetime(2026, 11, 20, tzinfo=UTC),
        18: datetime(2026, 11, 25, tzinfo=UTC),
        19: "Build and maintain production data pipelines.",
        20: "Data & Analytics",
        21: "full_time",
        22: ["en"],
        23: "job-1",
        24: "Data",
        25: "full_time",
        26: "permanent",
        27: "year",
        28: "observed",
        29: False,
        30: ["Python"],
        31: None,
        32: None,
        33: None,
        34: False,
        35: None,
        36: None,
        37: False,
        38: [],
        39: None,
        40: None,
        41: "NY",
        42: "10001",
        43: None,
        44: None,
        45: None,
        46: None,
        47: False,
        48: [],
        49: datetime(2026, 11, 25, tzinfo=UTC),
        50: "direct",
        51: site_domain,
    }
    for index, value in values.items():
        row[index] = value
    return row


def test_export_v2_includes_domain_and_explicit_salary_flag():
    record = _riga_attiva(_row(salary_min=100_000, salary_max=140_000), {})
    exported = json.loads(_riga(**record))

    assert exported["company_domain"] == "example.com"
    assert exported["has_salary"] is True
    assert "contact_email" not in exported
    assert "recruiter" not in exported


def test_export_v2_keeps_false_salary_flag_and_omits_missing_domain():
    record = _riga_attiva(_row(site_domain=None), {})
    exported = json.loads(_riga(**record))

    assert exported["has_salary"] is False
    assert "company_domain" not in exported
    assert "contact_email" not in exported
    assert "recruiter" not in exported
