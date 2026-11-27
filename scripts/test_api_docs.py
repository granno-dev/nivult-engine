from __future__ import annotations

import html
import json
import re
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "vetrina" / "account.html").read_text()
OPENAPI = json.loads((ROOT / "vetrina" / "openapi.json").read_text())


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
                "ats=greenhouse,Bundesagentur", "case-insensitively", "exclude_ats",
                "comma-separated lists", "exclusion wins", "complete schema sample",
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
        for marker in (
                "employees_source", "industry_source", "company_name_source", "size_range_source",
                "employees_min &lt;= employees_max", "Negative values, non-integer values",
                "inverted ranges return HTTP <code>400</code>"):
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
            "flusso_dal", "generato_at",
        }
        self.assertEqual(set(manifest), raw_keys | {"data", "righe", "file"})
        self.assertEqual(manifest["data"], manifest["data_export"])
        self.assertEqual(manifest["righe"]["offerte"], manifest["offerte"])
        self.assertEqual(manifest["file"], {
            "offerte": "/v1/exports/latest/offerte",
            "aziende": "/v1/exports/latest/aziende",
        })
        exports = section("doc-exports")
        for marker in (
                "jobs</code> (recommended)", "companies</code> (recommended)",
                "nivult-jobs.jsonl.gz", "nivult-companies.jsonl.gz",
                "fully backward-compatible aliases", "API-relative download paths",
                "Call these paths on the BASE URL", "/v1/exports/latest/{nome}"):
            self.assertIn(marker, exports)
        self.assertNotIn("/opt/", exports)
        self.assertNotIn("file_offerte", exports)
        self.assertNotIn("file_aziende", exports)

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
                "X-Credits-Charged: N", "Every direct paid response",
                "Actual credits charged to this request", "Only rate-limit 429 responses",
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
        self.assertIn("Salary and employee-range validation is 400", errors)
        self.assertIn("FastAPI typed parameter validation", errors)
        self.assertNotIn("non-integer <code>employees_min</code>", errors)
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


class ExecutableExamplesTest(unittest.TestCase):
    def test_every_python_example_compiles(self):
        examples = re.findall(r"python: `(?P<code>.*?)`\n  \}", DOC_EXAMPLES, re.DOTALL)
        self.assertEqual(len(examples), 7)
        for index, code in enumerate(examples):
            with self.subTest(example=index):
                compile(code, f"account-example-{index}.py", "exec")

    def test_every_curl_example_parses_in_bash(self):
        examples = re.findall(r"curl: `(?P<code>.*?)`,\n    python:", DOC_EXAMPLES, re.DOTALL)
        self.assertEqual(len(examples), 7)
        for index, code in enumerate(examples):
            with self.subTest(example=index):
                code = re.sub(r"\$\{[^}]+\}", "2026-11-27T00:00:00.000Z", code)
                result = subprocess.run(
                    ["bash", "-n"], input=code, text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_every_inline_script_passes_node_check(self):
        scripts = []
        for attrs, code in re.findall(
                r"<script(?P<attrs>[^>]*)>(?P<code>.*?)</script>", HTML, re.DOTALL):
            if "src=" not in attrs and "application/ld+json" not in attrs:
                scripts.append(code)
        self.assertTrue(scripts)
        for index, code in enumerate(scripts):
            with self.subTest(script=index):
                result = subprocess.run(
                    ["node", "--check", "-"], input=code, text=True,
                    capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_export_and_delta_examples_use_current_contract(self):
        exports = DOC_EXAMPLES[DOC_EXAMPLES.index("exports: {"):DOC_EXAMPLES.index("usage: {")]
        for marker in (
                "/v1/exports/latest/jobs", "/v1/exports/latest/companies",
                "nivult-jobs.jsonl.gz", "nivult-companies.jsonl.gz",
                'for name in ("jobs", "companies")'):
            self.assertIn(marker, exports)
        changes = DOC_EXAMPLES[DOC_EXAMPLES.index("changes: {"):DOC_EXAMPLES.index("exports: {")]
        self.assertIn("limit=5", changes)
        self.assertIn('"limit": 5', changes)
        self.assertNotIn("limit=100", changes)
        self.assertNotIn('"limit": 100', changes)


class OpenApiTest(unittest.TestCase):
    def resolve(self, reference):
        self.assertTrue(reference.startswith("#/"), reference)
        current = OPENAPI
        for part in reference[2:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            self.assertIn(part, current, reference)
            current = current[part]
        return current

    def response(self, path, status):
        operation = OPENAPI["paths"][path]["get"]
        response = operation["responses"][status]
        return self.resolve(response["$ref"]) if "$ref" in response else response

    def test_openapi_structure_and_every_reference_resolve(self):
        self.assertTrue(OPENAPI["openapi"].startswith("3."))
        operation_ids = set()

        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    self.assertIsInstance(value["$ref"], str)
                    self.resolve(value["$ref"])
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(OPENAPI)
        for path, item in OPENAPI["paths"].items():
            self.assertTrue(path.startswith("/v1/"))
            self.assertIn("get", item)
            operation = item["get"]
            self.assertNotIn(operation["operationId"], operation_ids)
            operation_ids.add(operation["operationId"])
            self.assertIsInstance(operation["responses"], dict)
            self.assertTrue(operation["responses"])
            for status, response in operation["responses"].items():
                self.assertRegex(status, r"^[1-5]\d\d$")
                resolved = self.resolve(response["$ref"]) if "$ref" in response else response
                self.assertIn("description", resolved)
                for parameter in operation.get("parameters", []):
                    if "$ref" not in parameter:
                        self.assertIn(parameter["in"], ("query", "path"))
                        self.assertIn("name", parameter)
                        self.assertIn("schema", parameter)

    def test_all_twelve_v1_paths_are_documented(self):
        self.assertEqual(set(OPENAPI["paths"]), {
            "/v1/jobs", "/v1/companies", "/v1/companies/{piattaforma}/{slug}",
            "/v1/changes", "/v1/exports/latest", "/v1/exports/latest/{nome}",
            "/v1/coverage", "/v1/usage", "/v1/demo/tecnologie", "/v1/demo/stats",
            "/v1/demo/sample-export", "/v1/demo/export-campione",
        })
        for path in OPENAPI["paths"]:
            if "/demo/" in path:
                self.assertEqual(OPENAPI["paths"][path]["get"]["security"], [])

    def test_jobs_parameter_surface_is_complete(self):
        parameters = OPENAPI["paths"]["/v1/jobs"]["get"]["parameters"]
        names = set()
        for parameter in parameters:
            parameter = self.resolve(parameter["$ref"]) if "$ref" in parameter else parameter
            names.add(parameter["name"])
        self.assertEqual(names, {
            "country", "category", "ats", "exclude_ats", "seniority", "remote", "language",
            "q", "q_any", "title", "technology", "source_type", "company", "company_slug",
            "company_domain", "city", "state", "employment_type", "department", "skills",
            "salary_currency", "has_salary", "salary_min", "salary_max", "posted_after",
            "posted_before", "dal", "limit", "cursor",
        })
        limit = self.resolve("#/components/parameters/PageLimit")["schema"]
        self.assertEqual((limit["default"], limit["minimum"], limit["maximum"]), (100, 1, 500))
        self.assertIn("capped at 25", self.resolve("#/components/parameters/PageLimit")["description"])

    def test_export_manifest_aliases_and_no_filesystem_paths(self):
        serialized = json.dumps(OPENAPI)
        self.assertNotIn("/opt/", serialized)
        self.assertNotIn("file_offerte", serialized)
        self.assertNotIn("file_aziende", serialized)
        parameter = OPENAPI["paths"]["/v1/exports/latest/{nome}"]["get"]["parameters"][0]
        self.assertEqual(parameter["schema"]["enum"], ["jobs", "companies", "offerte", "aziende"])
        response = self.response("/v1/exports/latest", "200")
        example = response["content"]["application/json"]["example"]
        self.assertEqual(example["file"], {
            "offerte": "/v1/exports/latest/offerte",
            "aziende": "/v1/exports/latest/aziende",
        })
        download = self.response("/v1/exports/latest/{nome}", "200")
        self.assertIn("Content-Disposition", download["headers"])

    def test_paid_and_unmetered_credit_headers(self):
        paid = (
            "/v1/jobs", "/v1/companies", "/v1/companies/{piattaforma}/{slug}", "/v1/changes",
        )
        for path in paid:
            for status in OPENAPI["paths"][path]["get"]["responses"]:
                with self.subTest(path=path, status=status):
                    self.assertIn("X-Credits-Charged", self.response(path, status)["headers"])
        for path in ("/v1/exports/latest", "/v1/exports/latest/{nome}", "/v1/coverage", "/v1/usage"):
            with self.subTest(path=path):
                self.assertIn("X-Credits-Charged", self.response(path, "200")["headers"])

    def test_retry_after_is_limited_to_rate_limit_429(self):
        for path, item in OPENAPI["paths"].items():
            for status in item["get"]["responses"]:
                headers = self.response(path, status).get("headers", {})
                if status != "429":
                    self.assertNotIn("Retry-After", headers, path)
        self.assertNotIn("Retry-After", self.response("/v1/demo/stats", "429").get("headers", {}))
        self.assertIn("Retry-After", self.response("/v1/jobs", "429")["headers"])
        self.assertIn("only for rate limits", self.response("/v1/jobs", "429")["description"])

    def test_employee_ranges_and_error_contract(self):
        parameters = OPENAPI["paths"]["/v1/companies"]["get"]["parameters"]
        for name in ("employees_min", "employees_max"):
            parameter = next(parameter for parameter in parameters if parameter["name"] == name)
            self.assertEqual(parameter["schema"]["minimum"], 0)
            self.assertIn("400", parameter["description"])
        error = self.response("/v1/companies", "400")
        self.assertIn("employee", error["description"])
        validation = self.response("/v1/companies", "422")
        self.assertNotIn("non-integer employees", validation["description"])

    def test_scale_usage_delta_and_credit_error_schemas(self):
        example = self.response("/v1/usage", "200")["content"]["application/json"]["example"]
        self.assertEqual(example["monthly_credits"], 500000)
        self.assertEqual(example["subscription_plan"], "scale")
        self.assertEqual(example["balance"], example["monthly_remaining"] + example["purchased_credits"])
        self.assertEqual(len(OPENAPI["components"]["schemas"]["Usage"]["required"]), 13)
        changes = self.response("/v1/changes", "200")["content"]["application/json"]["example"]
        self.assertEqual({event["event"] for event in changes["data"]}, {"new", "updated", "closed"})
        credit = OPENAPI["components"]["schemas"]["CreditError"]["properties"]["detail"]
        insufficient = OPENAPI["components"]["schemas"]["InsufficientCreditError"]["properties"]["detail"]
        self.assertIn("monthly_credits", credit["required"])
        self.assertIn("purchased_credits", credit["required"])
        self.assertIn("usage_month", credit["required"])
        self.assertNotIn("remaining", insufficient["required"] and credit["required"])
        retention = OPENAPI["components"]["schemas"]["DeltaRetentionResponse"]["properties"]["detail"]
        self.assertEqual(len(retention["required"]), 7)


if __name__ == "__main__":
    unittest.main()
