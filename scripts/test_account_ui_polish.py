from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "vetrina" / "account.html").read_text()


def source(start: str, end: str) -> str:
    return HTML[HTML.index(start):HTML.index(end, HTML.index(start))]


def luminance(color: str) -> float:
    channels = [int(color[index:index + 2], 16) / 255 for index in (1, 3, 5)]
    linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4
              for value in channels]
    return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))


def contrast(foreground: str, background: str) -> float:
    high, low = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class AccountUiPolishTest(unittest.TestCase):
    def test_quick_test_uses_the_safe_five_record_flow(self):
        quick = source("function curlPrimaChiamata()", "let quickLanguage")
        self.assertNotIn("limit=100", quick)
        self.assertNotIn('"limit": 100', quick)
        self.assertIn("country=DE&limit=5", quick)
        self.assertIn('"country": "DE", "limit": 5', quick)
        overview = source('<section id="overview"', '<section id="keys"')
        self.assertIn("API Reference →", overview)
        self.assertIn("Open Explorer →", overview)
        self.assertIn("apriApiReference()", overview)

    def test_credit_semantics_cover_zero_low_and_healthy_balances(self):
        functions = source("function creditRemainingPct(", "function disegnaAccesso()")
        result = subprocess.run(
            ["node", "-e", functions + """
const cases = [[0,500],[1,100],[20,100],[21,100],[0,0],[50,0]];
console.log(JSON.stringify(cases.map(([left,total]) => [creditProgressState(left,total), creditRemainingPct(left,total)])));
"""],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [
            ["depleted", 0], ["low", 1], ["low", 20], ["healthy", 21],
            ["depleted", 0], ["healthy", 100],
        ])
        drawing = source("function disegna()", "let chiaveFresca")
        for marker in (
                'data-credit-state="${creditState}"', 'role="progressbar"',
                'aria-valuenow=', 'stateLabel = creditState === "depleted" ? "Depleted"',
                'creditState === "low" ? "Low balance"', "0 left", "Top up credits →"):
            self.assertIn(marker, drawing if marker != "0 left" else HTML)
        self.assertIn("creditRemainingPct(disponibili, max + creditiExtra) < 15", drawing)
        self.assertIn(".credit-split.is-depleted > span { width: 0 !important; }", HTML)
        self.assertIn(".credit-split.is-low > span { background: #F59E0B; }", HTML)

    def test_shared_container_numeric_type_and_export_responsive_grid(self):
        self.assertIn('class="account-content p-4 sm:p-6 lg:p-8"', HTML)
        self.assertIn(".account-content { width: 100%; max-width: 1180px; margin-inline: auto; }", HTML)
        self.assertIn("body { font-variant-numeric: tabular-nums; }", HTML)
        self.assertIn("@media (min-width: 860px)", HTML)
        self.assertIn("grid-template-columns: minmax(0,1fr) minmax(300px,.92fr)", HTML)
        self.assertIn("max-height: calc(100dvh - 2rem)", HTML)
        self.assertIn("overflow-y: auto; overscroll-behavior: contain", HTML)
        self.assertIn("@media (max-width: 639px)", HTML)
        self.assertIn("height: 100dvh; max-height: 100dvh", HTML)

    def test_export_receipt_colors_meet_wcag_aa_on_navy(self):
        for color in ("#E2E8F0", "#94A3B8", "#60A5FA", "#F87171", "#34D399"):
            with self.subTest(color=color):
                self.assertGreaterEqual(contrast(color, "#0F172A"), 4.5)
                self.assertIn(color, HTML)
        self.assertIn('class="export-receipt"', HTML)
        self.assertIn("receipt-negative", HTML)

    def test_explorer_chips_presets_bridge_and_drawer_preview(self):
        for marker in (
                'id="explorer-export-slice"', "Export slice", 'id="explorer-filter-chips"',
                "SAP · DE", "Kubernetes · Remote", "Python · FR", "Clear all",
                "function exportExplorerSlice()", "function rimuoviFiltroExplorer(",
                "Transferred from Explorer:", "Not applied in Export:",
                'id="ex-transfer-note"', "Explorer filters transferred to Export"):
            self.assertIn(marker, HTML)
        bridge = source("function exportExplorerSlice()", "function aggiornaPulisci()")
        self.assertIn('pre = { tipo: tab === "jobs" ? "postings" : "companies" }', bridge)
        self.assertIn("apriExport(pre)", bridge)
        drawer = source("function revealValueCard(", "function copiaDominio(")
        for marker in (
                "Included in the free preview", "Locked preview", "Category", "Seniority",
                "Workplace", "Contract", "Language", "Published", "Source",
                "Verified technology stack", "Every active posting"):
            self.assertIn(marker, drawer)
        self.assertIn('saldoDisponibile() < 3', HTML)
        self.assertIn("Need 3 credits to reveal", HTML)

    def test_openapi_command_palette_toasts_and_custom_confirmation(self):
        self.assertIn('href="/openapi.json" target="_blank" rel="noopener"', HTML)
        self.assertIn("OpenAPI 3.1 JSON ↗", HTML)
        self.assertIn("Copy OpenAPI URL", HTML)
        self.assertIn("new URL(\"/openapi.json\", location.origin).href", HTML)
        palette = source("let commandQuery", 'document.querySelectorAll("[data-route-link]')
        for marker in (
                "...ROUTES.map", "API Reference", "Build filtered export", "Create new API key",
                "Top up credits", "Copy API Base URL", "Copy OpenAPI 3.1 URL",
                'event.metaKey || event.ctrlKey', '=== "k"', '"ArrowDown", "ArrowUp"',
                'event.key === "Enter"', 'event.key === "Escape"', "command.run()"):
            self.assertIn(marker, palette)
        self.assertIn('id="command-trigger"', HTML)
        self.assertIn('aria-keyshortcuts="Meta+K Control+K"', HTML)
        self.assertIn('close.setAttribute("aria-label", "Dismiss notification")', HTML)
        self.assertIn('close.addEventListener("click", () => el.remove())', HTML)
        self.assertIn("setTimeout(() => { if (el.isConnected) el.remove(); }, 6000)", HTML)
        self.assertIn('id="confirm-dialog"', HTML)
        self.assertIn('await chiediConferma("Revoke this API key?"', HTML)
        self.assertIn('await chiediConferma("Sign out other sessions?"', HTML)
        self.assertNotRegex(HTML, r"(?:window\.)?confirm\s*\(")

    def test_rate_limit_activity_affordance_and_session_layout(self):
        self.assertIn('id="api-rate-limit"', HTML)
        self.assertIn('volume <= 1000 ? "2 req/s · burst 5" : "15 req/s · burst 30"', HTML)
        for marker in (
                "recent-activity-head", ">Date</span>", ">Channel</span>",
                ">Details</span>", ">Credits</span>", "activity-company-link",
                "function apriAziendaDaAttivita(", "session-card-title", "session-card-detail",
                "CURRENT"):
            self.assertIn(marker, HTML)


if __name__ == "__main__":
    unittest.main()
