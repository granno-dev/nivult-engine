from __future__ import annotations

import functools
import importlib.util
import json
import os
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = Path(os.environ.get("NIVULT_ACCOUNT_ARTIFACTS", "/tmp/nivult-portal-exports-artifacts"))
VIEWPORTS = ((1440, 900), (1280, 800), (390, 844))


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        super().do_GET()

    def log_message(self, *_args):
        pass


@unittest.skipUnless(os.environ.get("NIVULT_ACCOUNT_BROWSER") == "1",
                     "set NIVULT_ACCOUNT_BROWSER=1 to run Chromium checks")
@unittest.skipUnless(importlib.util.find_spec("playwright") is not None,
                     "Playwright is required for Chromium checks")
class AccountExportsUxBrowserTest(unittest.TestCase):
    server = None
    server_thread = None
    base_url = None

    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        handler = functools.partial(Handler, directory=str(ROOT / "vetrina"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/account.html"

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        if cls.server_thread:
            cls.server_thread.join(timeout=5)

    def new_page(self, playwright, width, height):
        context = playwright.chromium.launch(headless=True).new_context(
            viewport={"width": width, "height": height},
            device_scale_factor=1,
            reduced_motion="reduce",
            permissions=["clipboard-read", "clipboard-write"],
        )
        context.add_init_script("sessionStorage.setItem('nv_sessione', 'browser-test-token')")
        page = context.new_page()
        console_errors, page_errors = [], []
        page.on("console", lambda message: console_errors.append(message.text)
                if message.type == "error" else None)
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.route("https://fonts.googleapis.com/**", lambda route: route.fulfill(
            status=200, content_type="text/css", body=""))
        page.route("https://fonts.gstatic.com/**", lambda route: route.abort())
        quotes = []

        def api_mock(route):
            request = route.request
            url = urlsplit(request.url)
            path, query = url.path, parse_qs(url.query)
            if path.startswith("/logo/"):
                route.fulfill(status=200, content_type="image/svg+xml",
                              body='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
                                   '<rect width="32" height="32" rx="8" fill="#2f5de0"/>'
                                   '<text x="16" y="21" text-anchor="middle" fill="white">A</text></svg>')
                return
            payload = json.loads(request.post_data or "{}") if request.method == "POST" else {}
            data = {}
            if path == "/me":
                data = {"nome": "Browser Test", "email": "browser@example.test", "stato": "active"}
            elif path == "/me/chiavi":
                data = {"chiavi": [], "extra": 0, "rivelazioni": 2, "prima_chiamata_fatta": True,
                        "saldo": {"crediti_mensili": 500, "usati": 0, "residui_mensili": 500,
                                  "crediti_extra": 0, "saldo": 500}, "abbonamento": {}}
            elif path == "/me/sicurezza":
                data = {"provider": [], "sessioni": []}
            elif path == "/portale/uso":
                data = {
                    "saldo": {"gratis": 500, "acquistati": 0, "spesi_mese": 3},
                    "per_via": {"portale": {"eventi": 1, "crediti": 3}},
                    "per_giorno": [], "non_dettagliati": 0, "registro_dal": "2026-10-04",
                    "eventi": [{"at": "2026-11-20T10:00:00+00:00", "via": "portale",
                                "azione": "company unlock", "dettaglio": "greenhouse:acme",
                                "crediti": 3, "titolo": "Acme"}],
                    "rivelazioni": [
                        {"ref": "greenhouse:acme", "at": "2026-11-20T10:00:00+00:00", "nome": "Acme",
                         "company_domain": "acme.example", "logo_url": "https://cdn.example/acme.svg",
                         "ats": "greenhouse", "company_slug": "acme"},
                        {"ref": "workday:fallback", "at": "2026-11-19T10:00:00+00:00", "nome": "Fallback Co",
                         "company_domain": None, "logo_url": None, "ats": "workday",
                         "company_slug": None},
                    ], "acquisti": [],
                }
            elif path == "/v1/demo/stats":
                data = {"offerte": 12345, "aziende": 678, "nuove_24h": 12}
            elif path in ("/portale/cerca", "/portale/cerca-aziende"):
                data = {"data": [], "next_cursor": None, "totale": 0}
            elif path == "/portale/token-azienda":
                data = {"reveal_token": "usage-token", "reveal_group": "usage-group"}
            elif path == "/portale/rivela-azienda":
                data = {"reveal_token": "usage-token", "reveal_group": "usage-group", "saldo": 497,
                        "azienda": {"company": "Acme", "name": "Acme", "company_slug": "acme",
                                    "ats": "greenhouse", "domain": "acme.example", "country": "IT",
                                    "industry": "software", "employees": 120, "active_jobs": 1,
                                    "jobs_posted_30d": 1, "locations": [{"city": "Milano", "country": "IT"}],
                                    "technologies": ["Python", "SAP"], "languages": ["en"],
                                    "description": "Acme builds test platforms."}}
            elif path == "/portale/azienda-jobs":
                data = {"data": [{"id": "job-1", "title": "Platform Engineer", "city": "Milano",
                                  "country": "IT", "posted_at": "2026-11-20T00:00:00"}]}
            elif path == "/portale/offerta-dettaglio":
                data = {"azienda_rivelata": True, "job_rivelata": True,
                        "company_ref": "greenhouse:acme", "reveal_token": "usage-token",
                        "reveal_group": "usage-group",
                        "offerta": {"id": "job-1", "title": "Platform Engineer", "revealed": True,
                                    "company": "Acme", "company_slug": "acme", "ats": "greenhouse",
                                    "company_domain": "acme.example", "country": "IT", "city": "Milano",
                                    "description": "Build reliable platforms.", "url": "https://acme.example/jobs/1",
                                    "technologies": ["Python"], "posted_at": "2026-11-20T00:00:00"}}
            elif path == "/portale/export/stima":
                quotes.append(payload)
                company = payload.get("tipo") == "companies"
                matching = 42
                maximum = payload.get("max_rows", 100000)
                rows = min(matching, maximum)
                unit = 3 if company else 1
                data = {"matching_rows": matching, "export_rows": rows, "unit_cost": unit,
                        "exact_cost": rows * unit, "affordable_rows": 500 // unit,
                        "max_allowed_rows": 100000, "max_rows": maximum,
                        "balance_after_export": 500 - rows * unit, "saldo": 500}
            elif path.startswith("/portale/catalogo/"):
                kind = path.rsplit("/", 1)[-1]
                values = {
                    "technology": [("Python", 48120), ("SAP", 38100), ("Kubernetes", 12040)],
                    "country": [("IT", 90000), ("DE", 85000), ("FR", 60000)],
                    "category": [("Engineering", 40000), ("Data & Analytics", 20000)],
                    "industry": [("software", 4000), ("finance", 2000)],
                    "ats": [("greenhouse", 50000), ("workday", 40000)],
                }.get(kind, [])
                needle = query.get("q", [""])[0].casefold()
                data = [{"value": value, "count": count} for value, count in values
                        if value.casefold().startswith(needle)]
            else:
                self.fail(f"Unexpected API request in browser test: {request.method} {path}")
            route.fulfill(status=200, content_type="application/json", body=json.dumps(data))

        page.route("https://api.nivult.com/**", api_mock)
        return context, page, console_errors, page_errors, quotes

    def finish_page(self, context, page, console_errors, page_errors):
        overflow = page.evaluate("""() => ({
          document: document.documentElement.scrollWidth - innerWidth,
          body: document.body.scrollWidth - innerWidth,
          dropdowns: [...document.querySelectorAll('.multi-combo-dropdown')].map(el => {
            const r = el.getBoundingClientRect(); return {left:r.left,right:r.right,width:innerWidth};
          })
        })""")
        self.assertLessEqual(overflow["document"], 1, overflow)
        self.assertLessEqual(overflow["body"], 1, overflow)
        for dropdown in overflow["dropdowns"]:
            self.assertGreaterEqual(dropdown["left"], 0)
            self.assertLessEqual(dropdown["right"], dropdown["width"])
        self.assertEqual(console_errors, [])
        self.assertEqual(page_errors, [])
        context.close()

    def test_usage_logos_geometry_and_in_place_drawer(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            for width, height in VIEWPORTS:
                with self.subTest(viewport=f"{width}x{height}"):
                    context, page, console_errors, page_errors, _ = self.new_page(
                        playwright, width, height)
                    suffix = f"{width}x{height}"
                    page.goto(self.base_url + "#usage", wait_until="networkidle")
                    page.locator(".unlocked-company-card").first.wait_for()
                    page.locator(".unlocked-company-card img.is-loaded").wait_for()
                    page.screenshot(path=str(ARTIFACTS / f"usage-before-{suffix}.png"), full_page=True)
                    geometry = page.locator("#unlocked-company-search").evaluate("""input => {
                      const icon = input.parentElement.querySelector('svg').getBoundingClientRect();
                      const box = input.getBoundingClientRect();
                      return {padding:parseFloat(getComputedStyle(input).paddingLeft),
                        iconRight:icon.right-box.left};
                    }""")
                    self.assertGreaterEqual(geometry["padding"], geometry["iconRight"] + 6)
                    cards = page.locator(".unlocked-company-card")
                    self.assertEqual(cards.count(), 2)
                    self.assertEqual(cards.locator("img[loading='lazy']").count(), 1)
                    self.assertEqual(cards.locator("img.is-loaded").count(), 1)
                    self.assertEqual(cards.nth(1).locator(".company-logo-monogram").inner_text(), "F")
                    cards.first.locator("img").evaluate("""img => {
                      img.dataset.logoIndex = '1'; img.dispatchEvent(new Event('error'));
                    }""")
                    self.assertEqual(cards.first.locator("img.is-loaded").count(), 0)
                    self.assertEqual(cards.first.locator(".company-logo-monogram").inner_text(), "A")

                    search = page.locator("#unlocked-company-search")
                    search.fill("Acme")
                    self.assertEqual(cards.count(), 1)
                    cards.first.scroll_into_view_if_needed()
                    before_scroll = page.evaluate("scrollY")
                    cards.first.click()
                    page.locator("#dettaglio.is-open").wait_for()
                    self.assertEqual(page.locator("#drawer-title").inner_text(), "Acme")
                    self.assertEqual(page.evaluate("location.hash"), "#usage")
                    page.screenshot(path=str(ARTIFACTS / f"usage-drawer-{suffix}.png"), full_page=True)
                    page.locator("#drawer-tab-tech").click()
                    self.assertIn("Python", page.locator("#drawer-panel").inner_text())
                    page.locator("#drawer-tab-jobs").click()
                    page.locator(".company-job-row").first.wait_for()
                    page.locator(".company-job-row").first.click()
                    page.get_by_role("button", name="Copy domain", exact=True).wait_for()
                    page.get_by_role("button", name="Copy domain", exact=True).click()
                    page.get_by_role("button", name="Copy API cURL", exact=True).click()
                    page.keyboard.press("Escape")
                    page.locator("#dettaglio").wait_for(state="hidden")
                    self.assertEqual(page.evaluate("location.hash"), "#usage")
                    self.assertEqual(search.input_value(), "Acme")
                    self.assertAlmostEqual(page.evaluate("scrollY"), before_scroll, delta=2)
                    page.screenshot(path=str(ARTIFACTS / f"usage-after-{suffix}.png"), full_page=True)
                    cards.first.click()
                    page.locator("#dettaglio.is-open").wait_for()
                    if width > 639:
                        page.locator(".explorer-drawer-backdrop").click(position={"x": 8, "y": 8})
                    else:
                        page.locator("#dettaglio-close").click()
                    page.locator("#dettaglio").wait_for(state="hidden")
                    self.assertEqual(page.evaluate("location.hash"), "#usage")
                    page.locator(".activity-company-link:visible").first.click()
                    page.locator("#dettaglio.is-open").wait_for()
                    self.assertEqual(page.evaluate("location.hash"), "#usage")
                    page.keyboard.press("Escape")
                    page.locator("#dettaglio").wait_for(state="hidden")
                    self.finish_page(context, page, console_errors, page_errors)

    def test_export_combobox_advanced_filters_companies_and_bridge(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            for width, height in VIEWPORTS:
                with self.subTest(viewport=f"{width}x{height}"):
                    context, page, console_errors, page_errors, quotes = self.new_page(
                        playwright, width, height)
                    suffix = f"{width}x{height}"
                    page.goto(self.base_url + "#exports", wait_until="networkidle")
                    page.get_by_role("button", name="Build a download", exact=True).click()
                    page.locator("#export-modale").wait_for()
                    tech = page.locator("#ex-tec")

                    def select_tech(query, tech=tech, page=page):
                        tech.fill(query)
                        page.wait_for_function("""() => {
                          const options = document.querySelectorAll('#ex-tec-listbox [role=option]');
                          return options.length === 1;
                        }""")
                        tech.press("ArrowDown")
                        tech.press("ArrowUp")
                        tech.press("Enter")

                    select_tech("py")
                    select_tech("sa")
                    self.assertEqual(page.evaluate("exportCombos.technologies.getValues()"), ["Python", "SAP"])
                    self.assertEqual(tech.get_attribute("role"), "combobox")
                    page.locator("#ex-tech-all").click()
                    filters = page.evaluate("filtriExport()")
                    self.assertEqual(filters["technologies"], ["Python", "SAP"])
                    self.assertEqual(filters["tech_match"], "all")
                    tech.press("Backspace")
                    self.assertEqual(page.evaluate("exportCombos.technologies.getValues()"), ["Python"])
                    page.locator(".multi-combo-chip button").first.click()
                    self.assertEqual(page.evaluate("exportCombos.technologies.getValues()"), [])
                    for query in ("py", "sa"):
                        select_tech(query)
                    tech.fill("DDDDDD")
                    page.get_by_text("No matching technology in index", exact=True).wait_for()
                    self.assertEqual(page.evaluate("exportCombos.technologies.getValues()"), ["Python", "SAP"])
                    self.assertNotIn("DDDDDD", page.evaluate("JSON.stringify(filtriExport())"))
                    page.screenshot(path=str(ARTIFACTS / f"export-combobox-{suffix}.png"))
                    tech.press("Escape")
                    page.locator("#export-modale").wait_for()
                    page.locator("#ex-more-filters > summary").click()
                    seniority = page.locator("#ex-seniority-input")
                    seniority.fill("sen")
                    page.wait_for_function("""() => document.querySelectorAll(
                      '#ex-seniority-input-listbox [role=option]').length === 1""")
                    seniority.press("Enter")
                    self.assertEqual(page.evaluate("exportCombos.seniority.getValues()"), ["senior"])
                    page.locator("#ex-has-salary").check()
                    page.locator("#ex-salary-min").fill("40000")
                    self.assertGreaterEqual(int(page.locator("#ex-advanced-badge").inner_text()), 2)
                    page.screenshot(path=str(ARTIFACTS / f"export-more-filters-{suffix}.png"))

                    page.locator("#ex-tipo-companies").click()
                    page.locator("#ex-companies-filters").wait_for()
                    page.get_by_role("button", name="51–200", exact=True).click()
                    page.locator("#ex-min-active-jobs").fill("3")
                    company_filters = page.evaluate("filtriExport()")
                    self.assertEqual(company_filters["employees_min"], 51)
                    self.assertEqual(company_filters["employees_max"], 200)
                    self.assertEqual(company_filters["min_active_jobs"], 3)
                    page.locator("#ex-tipo-postings").click()
                    page.evaluate("chiudiExport()")
                    page.evaluate("""() => {
                      location.hash = '#explorer';
                      applicaExplorerPreset('sap-de');
                      document.getElementById('f-q').value = 'Data Engineer';
                      document.getElementById('f-category').value = 'Engineering';
                      document.getElementById('f-seniority').value = 'senior';
                      document.getElementById('f-remoto').value = 'remote';
                      document.getElementById('f-language').value = 'en';
                      document.getElementById('f-ats').value = 'greenhouse';
                      document.getElementById('f-dal').value = '2026-11-01';
                      aggiornaPulisci();
                      exportExplorerSlice();
                    }""")
                    page.locator("#export-modale").wait_for()
                    bridge = page.evaluate("filtriExport()")
                    self.assertEqual(bridge["q"], "Data Engineer")
                    self.assertEqual(bridge["countries"], ["DE"])
                    self.assertEqual(bridge["technologies"], ["SAP"])
                    self.assertEqual(bridge["categories"], ["Engineering"])
                    self.assertEqual(bridge["seniorities"], ["senior"])
                    self.assertEqual(bridge["remote_values"], ["remote"])
                    self.assertEqual(bridge["languages"], ["en"])
                    self.assertEqual(bridge["ats"], ["greenhouse"])
                    self.assertEqual(bridge["posted_after"], "2026-11-01")
                    self.assertNotIn("Not applied", page.locator("#ex-transfer-note").inner_text())
                    self.assertTrue(quotes)
                    self.finish_page(context, page, console_errors, page_errors)


if __name__ == "__main__":
    unittest.main()
