from __future__ import annotations

import html
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "vetrina" / "account.html").read_text()


def section(section_id: str) -> str:
    start = HTML.index(f'<section id="{section_id}"')
    next_section = HTML.find("<section ", start + 1)
    return HTML[start:next_section if next_section != -1 else len(HTML)]


class JsonExampleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.examples: dict[str, str] = {}
        self._name: str | None = None
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "pre":
            return
        attributes = dict(attrs)
        self._name = attributes.get("data-json-example")
        self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._name is not None:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre" and self._name is not None:
            assert self._name not in self.examples
            self.examples[self._name] = "".join(self._chunks)
            self._name = None
            self._chunks = []


PARSER = JsonExampleParser()
PARSER.feed(HTML)
RAW_EXAMPLES = PARSER.examples
EXAMPLES = {name: json.loads(raw) for name, raw in RAW_EXAMPLES.items()}
DOC_EXAMPLES = HTML[HTML.index("const DOC_EXAMPLES = {"):HTML.index("function docCode(")]


class ApiDocsTest(unittest.TestCase):
    def test_every_marked_json_example_is_complete_and_valid(self):
        expected = {
            "empty-page", "jobs-page", "companies-page", "company-detail",
            "changes-page", "delta-retention-exceeded", "coverage", "export-manifest",
            "usage-scale", "rate-limit-error", "credits-exhausted-error",
            "insufficient-credit-error", "bad-salary-error", "unauthorized-error",
            "forbidden-error", "not-found-error", "validation-error",
            "service-unavailable-error",
        }
        self.assertEqual(set(RAW_EXAMPLES), expected)
        for name, raw in RAW_EXAMPLES.items():
            with self.subTest(example=name):
                self.assertNotIn("...", raw)
                self.assertIsInstance(EXAMPLES[name], dict)
        docs = HTML[HTML.index('<div class="doc-layout">'):HTML.index("</body>")]
        for match in re.finditer(r"<pre(?P<attrs>[^>]*)>(?P<body>\s*\{)", docs):
            self.assertIn("data-json-example=", match.group("attrs"))

    def test_doc_tabs_copy_buttons_and_examples_stay_wired(self):
        for example_id in (
                "quick", "jobs", "companies", "companyDetail", "changes", "exports", "usage"):
            with self.subTest(example=example_id):
                self.assertIn(f'data-doc-tabs="{example_id}"', HTML)
                self.assertIn(f'id="doc-{example_id}-code"', HTML)
                self.assertIn(f"copyDoc('doc-{example_id}-code',this)", HTML)
                self.assertIn(f"docCode('{example_id}','curl')", HTML)
                self.assertIn(f"docCode('{example_id}','python')", HTML)
                self.assertRegex(DOC_EXAMPLES, rf"\b{re.escape(example_id)}: \{{")
        self.assertIn('for (const id of Object.keys(DOC_EXAMPLES)) docCode(id, "curl")', HTML)
        self.assertIn("function copyDoc(id, btn)", HTML)
        self.assertIn("document.querySelectorAll(\"[data-doc-target]\")", HTML)

    def test_quickstart_is_zero_then_at_most_five_credits(self):
        intro = section("doc-intro")
        for marker in (
                "GET /v1/usage", "0 credits", "GET /v1/jobs?limit=5", "at most 5 credits",
                "max_pages"):
            self.assertIn(marker, intro)
        quick = DOC_EXAMPLES[DOC_EXAMPLES.index("quick: {"):DOC_EXAMPLES.index("jobs: {")]
        for marker in (
                '"/v1/usage"', "/v1/jobs?limit=5", "while True:", "response.raise_for_status()",
                'cursor = page["next_cursor"]', "if cursor is None:", "seen_cursors",
                "iter_jobs(max_pages=1)", 'params["cursor"] = cursor',
                '"Retry-After" in response.headers', "time.sleep(delay)",
                "2 ** attempt", "Stop immediately; never retry automatically"):
            self.assertIn(marker, quick)
        python = re.search(r"python: `import os\n(?P<code>.*?)`\n  \},", quick, re.DOTALL)
        self.assertIsNotNone(python)
        compile("import os\n" + python.group("code"), "quickstart.py", "exec")

    def test_jobs_filters_and_salary_contract(self):
        jobs = section("doc-jobs")
        for heading in (
                "Search &amp; Text", "Role, Skills &amp; Tech", "Location", "Company",
                "Compensation", "Source &amp; Dates", "Pagination"):
            self.assertIn(heading, jobs)
        self.assertNotIn("Core parameters", jobs)
        self.assertNotIn("v2 parameters", jobs)
        for marker in (
                "Data%20%26%20Analytics", "technology</code> versus <code>skills",
                "ats=bundesagentur", "case-sensitive", "exclude_ats", "token lists",
                "salary_min &gt;= N", "salary_max &lt;= N", "null <code>salary_min",
                "null <code>salary_max", "not annualized", "not normalized for gross or net",
                "salary_period", "not implicitly add", "annual gross", "return 400",
                "no <code>total_count</code>", "no facets or ATS-discovery endpoint",
                "no department discovery endpoint", "Unknown enum-like values"):
            self.assertIn(marker, jobs)
        job = EXAMPLES["jobs-page"]["data"][0]
        self.assertEqual(job["salary_period"], "year")
        self.assertTrue(job["has_salary"])
        self.assertNotIn("salary_estimate_min", job)
        self.assertGreater(len(job), 35)

    def test_full_category_and_enum_vocabularies(self):
        expected = [
            "Administrative", "Agriculture", "Art & Design", "Construction", "Consulting",
            "Creative & Media", "Customer Service & Support", "Data & Analytics", "Education",
            "Energy", "Engineering", "Environmental & Sustainability", "Finance & Accounting",
            "Food & Beverage", "Government & Public Sector", "Healthcare", "Hospitality",
            "Human Resources", "Legal", "Logistics", "Management & Leadership", "Manufacturing",
            "Marketing", "Retail", "Sales", "Science & Research", "Security & Safety",
            "Social Services", "Software", "Sports & Recreation", "Technology", "Trades",
            "Transportation",
        ]
        details = re.search(r'<details class="doc-details">(.*?)</details>', HTML, re.DOTALL)
        self.assertIsNotNone(details)
        categories = [html.unescape(value) for value in re.findall(r"<code>(.*?)</code>", details.group(1))]
        self.assertEqual(categories, expected)
        jobs = section("doc-jobs")
        for vocabulary in (
                "intern</code>, <code>junior</code>, <code>mid</code>, <code>senior</code>, <code>lead</code>, <code>head",
                "remote</code>, <code>hybrid</code>, <code>onsite",
                "full_time</code>, <code>part_time</code>, <code>contract</code>, <code>temporary</code>, <code>internship</code>, <code>apprenticeship",
                "direct</code>, <code>aggregator</code>, <code>staffing</code>, <code>public_board"):
            self.assertIn(vocabulary, jobs)

    def test_company_records_detail_and_provenance(self):
        companies = section("doc-companies")
        self.assertIn("do not comma-OR", companies)
        self.assertIn("no industry discovery endpoint", companies)
        self.assertIn("Optional empty or null top-level fields are omitted", companies)
        for marker in ("employees_source", "industry_source", "company_name_source", "size_range_source"):
            self.assertIn(marker, companies)
        summary = EXAMPLES["companies-page"]["data"][0]
        detail = EXAMPLES["company-detail"]
        self.assertEqual(summary["ats"], detail["ats"])
        self.assertEqual(summary["company_slug"], detail["company_slug"])
        self.assertNotIn("data", detail)
        self.assertEqual(detail["headquarters"]["source"], "company_site")
        self.assertIn("description_source", detail)
        self.assertIn("industry_source", detail)
        self.assertEqual(set(detail["keywords"]), {"famiglie", "tecnologie"})
        self.assertEqual(
            set(detail["locations"][0]),
            {"city", "country", "state", "offerte", "is_primary"},
        )
        self.assertIsInstance(detail["locations"][0]["offerte"], int)
        self.assertIsInstance(detail["locations"][0]["is_primary"], bool)
        self.assertIn("{piattaforma}", section("doc-company-detail"))

    def test_delta_events_and_retention_object(self):
        changes = EXAMPLES["changes-page"]["data"]
        self.assertEqual({event["event"] for event in changes}, {"new", "updated", "closed"})
        new, updated, closed = changes
        self.assertGreater(len(updated), len(new))
        self.assertIn("category", updated)
        self.assertNotIn("category", new)
        self.assertIn("closed_at", closed)
        retention = EXAMPLES["delta-retention-exceeded"]["detail"]
        self.assertEqual(
            set(retention),
            {"code", "error", "message", "since", "retention_since", "action", "resync_required"},
        )
        self.assertEqual(retention["code"], "delta_retention_exceeded")
        self.assertEqual(retention["action"], "resync")
        self.assertIs(retention["resync_required"], True)
        text = section("doc-changes")
        self.assertIn("Default retention is 7 days", text)
        self.assertIn("metadata-driven", text)
        self.assertIn("Full current active-job snapshot", text)
        self.assertIn("not a field-level patch", text)

    def test_coverage_and_manifest_include_every_contract_key(self):
        coverage = EXAMPLES["coverage"]
        job_keys = {
            "salary_observed", "salary_estimate", "description", "language", "category",
            "country", "skills", "employment_type", "seniority", "remote", "technologies",
            "summary", "benefits", "shift_schedule", "work_hours", "management_level",
            "valid_through", "state", "geo", "job_sources", "department", "salary_text",
        }
        company_keys = {
            "country", "domain", "website", "industry", "size_range", "employees", "legal_name",
            "legal_form", "registration_id", "founded", "headquarters", "description",
            "technologies", "top_skills", "locations", "external_urls", "lei", "languages",
        }
        self.assertEqual(set(coverage), job_keys | {"aziende"})
        self.assertEqual(set(coverage["aziende"]), company_keys)
        self.assertFalse(any(key.startswith("company:") for key in coverage))
        manifest = EXAMPLES["export-manifest"]
        raw_keys = {
            "stato", "data_export", "offerte", "aziende", "flusso", "flusso_giorni",
            "flusso_dal", "file_offerte", "file_aziende", "generato_at",
        }
        self.assertEqual(set(manifest), raw_keys | {"data", "righe", "file"})
        self.assertEqual(manifest["data"], manifest["data_export"])
        self.assertEqual(manifest["righe"]["offerte"], manifest["offerte"])
        self.assertEqual(manifest["file"]["offerte"], manifest["file_offerte"])
        exports = section("doc-exports")
        self.assertIn("Server paths are not URLs", exports)
        self.assertIn("offerte</code></td><td>Daily job postings", exports)
        self.assertIn("aziende</code></td><td>Daily companies", exports)
        self.assertIn("/v1/exports/latest/{nome}", exports)

    def test_usage_scale_arithmetic_and_all_fields(self):
        usage = EXAMPLES["usage-scale"]
        self.assertEqual(usage["monthly_credits"], 500000)
        self.assertEqual(usage["subscription_plan"], "scale")
        self.assertEqual(usage["remaining"], usage["balance"])
        self.assertEqual(usage["balance"], usage["monthly_remaining"] + usage["purchased_credits"])
        self.assertEqual(usage["monthly_credits"], usage["account_monthly_credits"])
        self.assertIn("current_period_end", usage)
        self.assertEqual(len(usage), 13)
        text = section("doc-usage")
        self.assertIn("<code>used</code> is account-wide", text)
        self.assertIn("<code>key_credits_used</code> is local", text)
        plans = section("doc-authentication")
        for marker in (
                "500 total trial credits; legacy accounts can differ", "15,000", "100,000",
                "500,000", "1,500,000", "floor(balance / credits_per_record)"):
            self.assertIn(marker, plans)

    def test_rate_limits_headers_and_three_429_payloads(self):
        rates = section("doc-rate-limits")
        for marker in (
                "2 requests/second", "15 requests/second", "5 requests", "30 requests",
                "in process", "Per API key", "RapidAPI", "X-Credits-Charged: 0",
                "Paid direct routes do not emit", "Only rate-limit 429 responses",
                "X-RapidAPI-Billing: Credits=N", "Content-Disposition", "X-RateLimit-*",
                "X-Content-Type-Options: nosniff", "Strict-Transport-Security",
                "X-Export-Rows", "X-Download-Id", "Inspect the JSON body before retrying",
                "stop immediately and never retry automatically", "retry with backoff",
                "No <code>/v1</code> endpoint returns 402"):
            self.assertIn(marker, rates)
        self.assertIsInstance(EXAMPLES["rate-limit-error"]["detail"], str)
        exhausted = EXAMPLES["credits-exhausted-error"]["detail"]
        self.assertEqual(exhausted["error"], "monthly and purchased credits exhausted")
        insufficient = EXAMPLES["insufficient-credit-error"]["detail"]
        self.assertEqual(insufficient["error"], "insufficient credits for one result")
        errors = section("doc-errors")
        self.assertIn("Salary validation is 400", errors)
        self.assertIn("FastAPI typed parameter validation", errors)
        for status in ("400", "401", "403", "404", "422", "429", "503"):
            self.assertIn(f"<td>{status}</td>", errors)
        self.assertIn("What to do", errors)

    def test_responsive_doc_css_keeps_overflow_inside_components(self):
        css = HTML[HTML.index(".doc-layout {"):HTML.index("#billing:not(.hidden)")]
        for marker in (
                ".doc-layout > * { min-width: 0; }", ".doc-section { min-width: 0;",
                ".doc-table-wrap { max-width: 100%;", "overflow-x: auto",
                ".doc-section .code-block { max-width: 100%; }", "overflow-wrap: anywhere",
                "@media (max-width: 960px)"):
            self.assertIn(marker, css)


if __name__ == "__main__":
    unittest.main()
