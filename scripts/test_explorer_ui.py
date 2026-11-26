import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "vetrina" / "account.html").read_text()


def section(start: str, end: str) -> str:
    return HTML[HTML.index(start):HTML.index(end, HTML.index(start))]


class ExplorerUiTest(unittest.TestCase):
    def test_revealed_logo_prefers_local_and_never_removes_visual(self):
        source = section("function logoRivelato(", "function aggiornaRigheRivelate(")
        self.assertIn("record.revealed !== true", source)
        self.assertLess(source.index("const local"), source.index("const remote"))
        self.assertIn("`${API}/logo/${encodeURIComponent(record.company_slug)}`", source)
        self.assertIn("urlSicuro(record.logo_url) || urlSicuro(record.logo)", source)
        self.assertIn("company-logo-monogram", source)
        self.assertIn('esc(JSON.stringify(sources))', source)
        self.assertIn("next < sources.length", source)
        self.assertIn("img.src = sources[next]", source)
        self.assertNotIn("onerror=", source)
        self.assertNotIn("this.remove", source)
        self.assertNotIn("img.remove", source)

    def test_all_company_logo_surfaces_use_shared_renderer(self):
        for start, end in [
            ("function rigaJob(", "function rigaAzienda("),
            ("function rigaAzienda(", "function cambiaTab("),
            ("async function dettaglioJob(", "async function apriAziendaDaJob("),
            ("async function mostraAzienda(", "function dettaglioAzienda("),
        ]:
            with self.subTest(surface=start):
                self.assertIn("logoRivelato(", section(start, end))

    def test_job_reveal_and_revealed_company_card_are_light_and_safe(self):
        source = section("async function dettaglioJob(", "async function apriAziendaDaJob(")
        self.assertIn("company-reveal-card", source)
        self.assertIn("locked-posting-card", source)
        self.assertIn("3 credits once · permanent access", source)
        self.assertIn("company-summary-card", source)
        self.assertIn("Open company profile", source)
        self.assertIn("Open original posting", source)
        self.assertIn("urlSicuro(o.url)", source)
        self.assertIn("event.stopPropagation()", source)
        self.assertIn('data-job-id="${esc(o.id)}"', source)
        self.assertIn("rivelaAzienda(${jsArg(o.reveal_token)}, this)", source)
        self.assertNotIn("bg-slate-950", source)
        self.assertNotIn("from-indigo-600", source)

    def test_company_profile_has_readable_separate_data_bars(self):
        source = section("async function mostraAzienda(", "function _quando(")
        self.assertGreaterEqual(source.count("company-profile-header"), 2)
        self.assertIn("company-kpi-value", source)
        self.assertIn("company-tech-row", source)
        self.assertIn("company-tech-track", source)
        self.assertIn("font-bold text-slate-800", source)
        self.assertIn("font-bold text-slate-700", source)
        self.assertIn("company-location-row", source)
        self.assertIn("company-job-row", source)
        self.assertIn("3 credits once · permanent access", source)
        self.assertNotIn("bg-slate-950", source)
        self.assertIn('class="modal-close" aria-label="Close details"', HTML)


if __name__ == "__main__":
    unittest.main()
