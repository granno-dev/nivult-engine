from __future__ import annotations

import functools
import importlib.util
import json
import os
import re
import subprocess
import threading
import unittest
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "vetrina" / "index.html"
HTML = HTML_PATH.read_text()
ARTIFACTS = Path(os.environ.get("NIVULT_LANDING_ARTIFACTS", "/tmp/nivult-landing-artifacts"))
VIEWPORTS = [(375, 812), (390, 844), (768, 1024), (1280, 800), (1440, 900)]


class Scripts(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        if tag != "script":
            return
        attrs = dict(attrs)
        self.current = {"type": attrs.get("type", ""), "src": attrs.get("src"), "text": []}

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.current["text"] = "".join(self.current["text"])
            self.items.append(self.current)
            self.current = None


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        pass


def source(start: str, end: str) -> str:
    return HTML[HTML.index(start):HTML.index(end, HTML.index(start))]


class LandingSourceTest(unittest.TestCase):
    def test_inline_javascript_and_json_ld_are_valid(self):
        parser = Scripts()
        parser.feed(HTML)
        for index, script in enumerate(parser.items):
            if script["src"]:
                continue
            if script["type"] == "application/ld+json":
                data = json.loads(script["text"])
                faq = next(item for item in data["@graph"] if item["@type"] == "FAQPage")
                self.assertEqual(len(faq["mainEntity"]), 7)
                continue
            result = subprocess.run(
                ["node", "--check"], input=script["text"], text=True,
                capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, f"inline script {index}: {result.stderr}")

    def test_pricing_matches_account_and_backend_contract(self):
        account = (ROOT / "vetrina" / "account.html").read_text()
        expected = {
            "starter": ("49", "490", "15000"),
            "growth": ("149", "1490", "100000"),
            "scale": ("449", "4490", "500000"),
            "business": ("899", "8990", "1500000"),
        }
        for plan, (month, year, credits) in expected.items():
            marker = (f'data-plan="{plan}" data-month="{month}" data-year="{year}" '
                      f'data-credits="{credits}"')
            self.assertIn(marker, HTML)
            self.assertIn(f'data-credits="{credits}" data-month="{month}" data-year="{year}"', account)
        self.assertIn("Included in every paid plan: 15 req/s API speed · burst 30", HTML)
        self.assertIn("2 req/s · burst 5", HTML)
        self.assertIn("€4 per 1,000 credits, €40 minimum", HTML)
        self.assertIn("0.90", source('data-plan="scale"', 'data-plan="business"'))
        self.assertIn("0.60", source('data-plan="business"', "<!--"))

    def test_pricing_copy_avoids_banned_jargon(self):
        pricing = source('<section id="pricing"', '<section id="faq"')
        visible_pricing = re.sub(r"<[^>]+>", "", pricing).lower()
        for word in ("error", "errors", "entitlements", "onboarding", "(", ")"):
            self.assertNotIn(word, visible_pricing)
        for marker in (
                "Only pay", "for returned records", "Included in every paid plan",
                "For testing &amp; small projects", "Up to 15K jobs or 5K companies",
                "Everything in Starter, plus:", "Everything in Growth, plus:",
                "Everything in Scale, plus:", "Request missing companies",
                "Add up to 25 companies / mo", "Add up to 100 companies / mo",
                "Unlimited company additions &amp; custom sources",
                "Dedicated engineering support"):
            self.assertIn(marker, pricing)
        self.assertEqual(pricing.count('aria-hidden="true">✓</span>'), 11)

    def test_commercial_content_and_links_are_present(self):
        self.assertEqual(HTML.count('class="faq-item '), 7)
        self.assertEqual(HTML.count("<article class=\"reveal lift rounded-2xl"), 4)
        for marker in (
                "Who builds on Nivult", "Request missing companies",
                "48h turnaround on requests", "Direct Slack &amp; email support",
                "Unlimited company additions &amp; custom sources",
                "Live index preview — company names unlock inside the portal.", "Also available on",
                "https://apify.com/nivult/global-job-postings",
                "https://rapidapi.com/nivult-nivult-default/api/nivult-job-postings-company-data",
                'href="/account"', 'href="/terms"', 'href="/privacy"', 'href="/ats/"'):
            self.assertIn(marker, HTML)
        self.assertIn("Versioned REST endpoints, cursor pagination, and zero PII by design.", HTML)
        self.assertIn("Live index snapshot · updated daily", HTML)
        self.assertEqual(HTML.count('target="_blank" rel="noopener noreferrer"'), 3)


@unittest.skipUnless(os.environ.get("NIVULT_LANDING_BROWSER") == "1",
                     "set NIVULT_LANDING_BROWSER=1 to run Chromium checks")
@unittest.skipUnless(importlib.util.find_spec("playwright") is not None,
                     "Playwright is required for Chromium checks")
class LandingChromiumTest(unittest.TestCase):
    results: ClassVar[dict] = {}
    server = None
    server_thread = None
    base_url = None

    @classmethod
    def setUpClass(cls):
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        handler = functools.partial(QuietHandler, directory=str(ROOT / "vetrina"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}/"

    @classmethod
    def tearDownClass(cls):
        if cls.server:
            cls.server.shutdown()
            cls.server.server_close()
        if cls.server_thread:
            cls.server_thread.join(timeout=5)
        ARTIFACTS.mkdir(parents=True, exist_ok=True)
        (ARTIFACTS / "chromium-report.json").write_text(
            json.dumps(cls.results, indent=2, ensure_ascii=False))

    def check_layout(self, page, width):
        issues = page.evaluate("""() => {
          const selectors = [
            '.hero-section > .max-w-7xl', '.hero-mobile-record', '.stat-num',
            '.provenance-table', '#changes > .max-w-7xl', '#query-widget',
            '.pricing-grid > article', '.plan-addons-grid > article',
            '#api .grid > div', 'footer .relative.max-w-7xl'
          ];
          const issues = [];
          for (const el of document.querySelectorAll(selectors.join(','))) {
            const style = getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden') continue;
            const rect = el.getBoundingClientRect();
            if (rect.left < -1 || rect.right > innerWidth + 1) {
              issues.push({kind:'bounds', selector:el.className, left:rect.left, right:rect.right});
            }
            if (el.scrollWidth > el.clientWidth + 1 && !['auto','scroll'].includes(style.overflowX)) {
              issues.push({kind:'clipped', selector:el.className,
                clientWidth:el.clientWidth, scrollWidth:el.scrollWidth, overflowX:style.overflowX});
            }
          }
          for (const el of document.querySelectorAll('pre.codebox')) {
            const style = getComputedStyle(el), rect = el.getBoundingClientRect();
            if (!['auto','scroll'].includes(style.overflowX)) issues.push({kind:'pre-overflow', id:el.id});
            if (rect.left < -1 || rect.right > innerWidth + 1) issues.push({kind:'pre-bounds', id:el.id});
          }
          if (innerWidth <= 640) {
            for (const el of document.querySelectorAll('#changes .truncate')) {
              if (getComputedStyle(el).whiteSpace === 'nowrap' || el.scrollWidth > el.clientWidth + 1) {
                issues.push({kind:'delta-truncation', text:el.innerText});
              }
            }
          }
          return issues;
        }""")
        self.assertEqual(issues, [], f"layout issues at {width}px: {issues}")

    def capture_sections(self, page, viewport):
        sections = ["changes", "data", "use-cases", "coverage", "api", "pricing", "faq", "contact"]
        for section in sections:
            locator = page.locator(f"#{section}")
            locator.scroll_into_view_if_needed()
            page.wait_for_timeout(150)
            if viewport in ("375x812", "390x844", "768x1024"):
                overlaps = page.evaluate("""section => {
                  const pillEl = document.querySelector('#pill-booking');
                  if (getComputedStyle(pillEl).opacity === '0') return [];
                  const pill = pillEl.getBoundingClientRect();
                  return [...document.querySelectorAll(`#${section} a, #${section} button, #${section} p, #${section} h1, #${section} h2, #${section} h3, #${section} li, #${section} pre`)].flatMap(el => {
                    const r = el.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return [];
                    const overlap = r.left < pill.right && r.right > pill.left && r.top < pill.bottom && r.bottom > pill.top;
                    return overlap ? [{section, text:(el.innerText || '').slice(0,100)}] : [];
                  });
                }""", section)
                self.assertEqual(overlaps, [], f"floating CTA overlaps {section} at {viewport}: {overlaps}")
            page.evaluate("document.querySelector('#site-nav').style.visibility='hidden';document.querySelector('#pill-booking').style.visibility='hidden'")
            locator.screenshot(path=str(ARTIFACTS / f"landing-{viewport}-{section}.png"))
            page.evaluate("document.querySelector('#site-nav').style.visibility='';document.querySelector('#pill-booking').style.visibility=''")
        page.evaluate("document.querySelector('#site-nav').style.visibility='hidden'")
        page.locator("footer").screenshot(path=str(ARTIFACTS / f"landing-{viewport}-footer.png"))
        page.evaluate("document.querySelector('#site-nav').style.visibility=''")

    def test_five_viewports(self):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            for width, height in VIEWPORTS:
                viewport = f"{width}x{height}"
                with self.subTest(viewport=viewport):
                    page = browser.new_page(
                        viewport={"width": width, "height": height},
                        device_scale_factor=1, reduced_motion="reduce",
                    )
                    console_errors, page_errors = [], []
                    page.on("console", lambda msg, errors=console_errors: (
                        errors.append(msg.text) if msg.type == "error" else None))
                    page.on("pageerror", lambda exc, errors=page_errors: errors.append(str(exc)))
                    result = {"consoleErrors": [], "pageErrors": []}
                    self.results[viewport] = result
                    try:
                        page.route("https://api.nivult.com/v1/demo/tecnologie?*", lambda route: route.fulfill(
                            status=200, content_type="application/json",
                            body=json.dumps({
                                "query": "SAP",
                                "data": [{"country": "FR", "industry": "consumer goods", "technologies": ["SAP"]}],
                                "altre": True,
                            })))
                        page.goto(self.base_url, wait_until="networkidle", timeout=45000)
                        initial = page.evaluate("""() => ({
                          scrollWidth: document.documentElement.scrollWidth,
                          clientWidth: document.documentElement.clientWidth
                        })""")
                        result.update(initial)
                        self.assertLessEqual(initial["scrollWidth"], initial["clientWidth"])
                        self.check_layout(page, width)
                        floating_initial = page.locator("#pill-booking").evaluate("""el => ({
                          opacity:getComputedStyle(el).opacity,
                          ariaHidden:el.getAttribute('aria-hidden')
                        })""")
                        self.assertEqual(floating_initial["opacity"], "0")
                        self.assertEqual(floating_initial["ariaHidden"], "true")
                        pricing_text = page.locator("#pricing").inner_text().lower()
                        faq_text = page.locator("#faq").inner_text().lower()
                        for word in ("error", "errors", "entitlements", "onboarding", "(", ")"):
                            self.assertNotIn(word, pricing_text)
                        for word in ("error", "errors", "entitlements", "onboarding"):
                            self.assertNotIn(word, faq_text)
                        for marker in (
                                "only pay for returned records", "request missing companies",
                                "included in every paid plan"):
                            self.assertIn(marker, pricing_text)
                        if width >= 1280:
                            feature_lines = page.locator(".pricing-grid [data-plan-card] ul li").evaluate_all(
                                "els => els.map(el => ({text:el.innerText, height:el.getBoundingClientRect().height}))")
                            self.assertEqual(len(feature_lines), 11)
                            self.assertTrue(all(item["height"] <= 24 for item in feature_lines), feature_lines)
                        hero_feeds = page.evaluate("""() => [...document.querySelectorAll('.hero-feed-card')].flatMap(card => {
                          const cardRect = card.getBoundingClientRect();
                          if (getComputedStyle(card).display === 'none' || cardRect.width === 0) return [];
                          const rows = [...card.querySelectorAll('[data-hero-feed-row]')].filter(row => {
                            const rect = row.getBoundingClientRect();
                            return getComputedStyle(row).display !== 'none' && rect.height > 0;
                          });
                          const company = [...document.querySelectorAll('.float-a, .hero-mobile-record')].find(el => {
                            const rect = el.getBoundingClientRect();
                            return getComputedStyle(el).display !== 'none' && rect.width > 0;
                          });
                          const companyRect = company.getBoundingClientRect();
                          return [{
                            rowCount: rows.length,
                            cardBottom: cardRect.bottom,
                            violations: rows.flatMap(row => {
                              const rect = row.getBoundingClientRect();
                              return rect.bottom > cardRect.bottom - 8
                                ? [{text:row.innerText, rowBottom:rect.bottom}] : [];
                            }),
                            overlaps: rows.flatMap(row => {
                              const rect = row.getBoundingClientRect();
                              const overlap = rect.left < companyRect.right && rect.right > companyRect.left
                                && rect.top < companyRect.bottom && rect.bottom > companyRect.top;
                              return overlap ? [{text:row.innerText, rowTop:rect.top, companyBottom:companyRect.bottom}] : [];
                            })
                          }];
                        })""")
                        result["heroFeed"] = hero_feeds
                        self.assertEqual(len(hero_feeds), 1)
                        self.assertEqual(hero_feeds[0]["rowCount"], 3)
                        self.assertEqual(hero_feeds[0]["violations"], [])
                        self.assertEqual(hero_feeds[0]["overlaps"], [])
                        faq_alignment = page.evaluate("""() => {
                          const normalize = value => value.replace(/\\s+/g, ' ').trim();
                          const html = [...document.querySelectorAll('.faq-answer')].map(el => normalize(el.textContent));
                          const schema = JSON.parse(document.querySelector('script[type="application/ld+json"]').textContent);
                          const faq = schema['@graph'].find(item => item['@type'] === 'FAQPage');
                          const jsonLd = faq.mainEntity.map(item => normalize(item.acceptedAnswer.text));
                          return {html, jsonLd};
                        }""")
                        self.assertEqual(faq_alignment["html"], faq_alignment["jsonLd"])
                        self.assertEqual(len(faq_alignment["html"]), 7)
                        if width >= 1280:
                            subtitle = page.locator(".hero-subtitle").evaluate("""el => ({
                              lines: el.getBoundingClientRect().height / parseFloat(getComputedStyle(el).lineHeight),
                              words: el.innerText.trim().split(/\\s+/).length
                            })""")
                            self.assertLessEqual(subtitle["lines"], 2)
                            self.assertGreaterEqual(subtitle["words"], 24)
                            self.assertLessEqual(subtitle["words"], 30)
                        page.screenshot(path=str(ARTIFACTS / f"landing-{viewport}-hero.png"))
                        page.screenshot(path=str(ARTIFACTS / f"landing-{viewport}-full.png"), full_page=True)

                        hamburger = page.locator("#hamburger")
                        if width <= 900:
                            self.assertTrue(hamburger.is_visible())
                            box = hamburger.bounding_box()
                            self.assertGreaterEqual(box["width"], 44)
                            self.assertGreaterEqual(box["height"], 44)
                            self.assertEqual(hamburger.get_attribute("aria-controls"), "mobile-menu")
                            hamburger.click()
                            self.assertEqual(hamburger.get_attribute("aria-expanded"), "true")
                            self.assertTrue(page.locator("#mobile-menu").is_visible())
                            self.assertTrue(page.locator("body").evaluate("el => el.classList.contains('menu-open')"))
                            self.assertEqual(page.locator("#mobile-menu a").count(), 7)
                            if viewport == "390x844":
                                page.screenshot(path=str(ARTIFACTS / f"landing-{viewport}-menu-open.png"))
                            page.keyboard.press("Escape")
                            self.assertEqual(hamburger.get_attribute("aria-expanded"), "false")
                            self.assertFalse(page.locator("body").evaluate("el => el.classList.contains('menu-open')"))
                            hamburger.click()
                            page.locator('#mobile-menu a[href="#data"]').click()
                            self.assertEqual(hamburger.get_attribute("aria-expanded"), "false")
                            hamburger.click()
                            page.mouse.click(width - 5, min(height - 10, 700))
                            self.assertEqual(hamburger.get_attribute("aria-expanded"), "false")
                        else:
                            self.assertFalse(hamburger.is_visible())
                            self.assertTrue(page.locator("#site-nav .desktop-nav").is_visible())

                        page.locator("#pricing").scroll_into_view_if_needed()
                        page.wait_for_timeout(150)
                        floating_pricing = page.locator("#pill-booking").evaluate("""el => ({
                          opacity:getComputedStyle(el).opacity,
                          ariaHidden:el.getAttribute('aria-hidden'),
                          position:getComputedStyle(el).position,
                          width:el.getBoundingClientRect().width,
                          height:el.getBoundingClientRect().height,
                          right:getComputedStyle(el).right,
                          bottom:getComputedStyle(el).bottom,
                          labelDisplay:getComputedStyle(el.querySelector('.pill-label')).display
                        })""")
                        self.assertEqual(floating_pricing["opacity"], "1")
                        self.assertEqual(floating_pricing["ariaHidden"], "false")
                        self.assertEqual(floating_pricing["position"], "fixed")
                        if width <= 768:
                            self.assertEqual(floating_pricing["width"], 50)
                            self.assertEqual(floating_pricing["height"], 50)
                            self.assertEqual(floating_pricing["right"], "16px")
                            self.assertEqual(floating_pricing["bottom"], "16px")
                            self.assertEqual(floating_pricing["labelDisplay"], "none")
                            hamburger.click()
                            self.assertEqual(page.locator("#pill-booking").get_attribute("aria-hidden"), "true")
                            self.assertEqual(page.locator("#pill-booking").evaluate("el => getComputedStyle(el).visibility"), "hidden")
                            page.keyboard.press("Escape")
                            self.assertEqual(page.locator("#pill-booking").get_attribute("aria-hidden"), "false")
                            self.assertEqual(page.locator("#pill-booking").evaluate("el => getComputedStyle(el).opacity"), "1")
                        else:
                            self.assertEqual(floating_pricing["right"], "20px")
                            self.assertEqual(floating_pricing["bottom"], "20px")
                            self.assertNotEqual(floating_pricing["labelDisplay"], "none")

                        page.locator("#billing-year").click()
                        annual = {
                            "starter": ("€490", "€2.72 / 1K credits"),
                            "growth": ("€1,490", "€1.24 / 1K credits"),
                            "scale": ("€4,490", "€0.75 / 1K credits"),
                            "business": ("€8,990", "€0.50 / 1K credits"),
                        }
                        for plan, (price, unit) in annual.items():
                            card = page.locator(f'[data-plan="{plan}"]')
                            self.assertEqual(card.locator("[data-price]").inner_text(), price)
                            self.assertEqual(card.locator("[data-unit-price]").inner_text(), unit)
                            self.assertEqual(card.locator("[data-price-suffix]").inner_text(), "/ year")
                        self.assertEqual(page.locator("#billing-year").get_attribute("aria-pressed"), "true")
                        if viewport == "390x844":
                            page.locator("#pricing").screenshot(path=str(ARTIFACTS / f"landing-{viewport}-pricing-annual.png"))
                        page.locator("#billing-month").click()
                        monthly = {"starter": "€49", "growth": "€149", "scale": "€449", "business": "€899"}
                        for plan, price in monthly.items():
                            self.assertEqual(page.locator(f'[data-plan="{plan}"] [data-price]').inner_text(), price)
                        self.assertEqual(page.locator("#billing-month").get_attribute("aria-pressed"), "true")
                        if viewport == "390x844":
                            page.locator("#pricing").screenshot(path=str(ARTIFACTS / f"landing-{viewport}-pricing.png"))

                        faqs = page.locator(".faq-item")
                        self.assertEqual(faqs.count(), 7)
                        for index in range(7):
                            trigger = faqs.nth(index).locator(".faq-trigger")
                            trigger.click()
                            self.assertEqual(trigger.get_attribute("aria-expanded"), "true")
                            self.assertGreater(faqs.nth(index).locator(".faq-panel").evaluate("el => el.scrollHeight"), 0)
                            trigger.click()
                            self.assertEqual(trigger.get_attribute("aria-expanded"), "false")
                        self.assertGreater(page.evaluate("scrollY"), 240)
                        self.assertEqual(page.locator("#pill-booking").get_attribute("aria-hidden"), "false")
                        self.assertEqual(page.locator("#pill-booking").evaluate("el => getComputedStyle(el).opacity"), "1")

                        page.locator("#q").fill("SAP")
                        page.wait_for_function("document.querySelector('#demo-fonte').textContent === 'live'")
                        self.assertGreater(page.locator("#risultati").get_by_text("Hidden company", exact=True).count(), 0)
                        self.assertTrue(page.get_by_text("Live index preview — company names unlock inside the portal.").is_visible())
                        page.locator("#tab-off").click()
                        self.assertEqual(page.locator("#tab-off").get_attribute("aria-selected"), "true")
                        if width <= 900:
                            self.assertGreaterEqual(float(page.locator("#q").evaluate("el => parseFloat(getComputedStyle(el).fontSize)")), 16)

                        columns = page.locator(".pricing-grid").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
                        expected_columns = 1 if width <= 768 else (2 if width <= 1100 else 4)
                        self.assertEqual(columns, expected_columns)
                        addon_columns = page.locator(".plan-addons-grid").evaluate("el => getComputedStyle(el).gridTemplateColumns.split(' ').length")
                        self.assertEqual(addon_columns, expected_columns)
                        if width <= 390:
                            hero_bottom = page.locator("#hero-trial").evaluate("el => el.getBoundingClientRect().bottom + scrollY")
                            hero_top = page.locator(".hero-section").evaluate("el => el.getBoundingClientRect().top + scrollY")
                            self.assertLessEqual(hero_bottom - hero_top, height)

                        page.evaluate("scrollTo(0, document.documentElement.scrollHeight)")
                        page.wait_for_timeout(200)
                        overlaps = page.evaluate("""() => {
                          const pill = document.querySelector('#pill-booking').getBoundingClientRect();
                          return [...document.querySelectorAll('footer a, footer p, footer span')].flatMap(el => {
                            const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                            if (s.display === 'none' || r.width === 0 || r.height === 0) return [];
                            const overlap = r.left < pill.right && r.right > pill.left && r.top < pill.bottom && r.bottom > pill.top;
                            return overlap ? [{text:(el.innerText || '').slice(0,100), rect:r.toJSON()}] : [];
                          });
                        }""")
                        self.assertEqual(overlaps, [], f"floating CTA overlaps footer at {viewport}: {overlaps}")
                        self.check_layout(page, width)
                        final = page.evaluate("""() => ({
                          scrollWidth: document.documentElement.scrollWidth,
                          clientWidth: document.documentElement.clientWidth
                        })""")
                        result["finalScrollWidth"] = final["scrollWidth"]
                        self.assertLessEqual(final["scrollWidth"], final["clientWidth"])
                        if viewport in ("390x844", "1440x900"):
                            self.capture_sections(page, viewport)
                        self.assertEqual(console_errors, [])
                        self.assertEqual(page_errors, [])
                        result["status"] = "passed"
                    except Exception as exc:
                        result["status"] = "failed"
                        result["failure"] = str(exc)
                        page.screenshot(path=str(ARTIFACTS / f"landing-{viewport}-failure.png"), full_page=True)
                        raise
                    finally:
                        result["consoleErrors"] = console_errors
                        result["pageErrors"] = page_errors
                        (ARTIFACTS / "chromium-report.json").write_text(
                            json.dumps(self.results, indent=2, ensure_ascii=False))
                        page.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
