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
        self.assertIn("Posting preview", source)
        self.assertIn("o.description_preview", source)
        self.assertIn("Free to open", source)
        self.assertIn("Unlock full job · 1 credit", source)
        self.assertIn("rivelaJob(${jsArg(o.id)}, this)", source)
        self.assertIn("const jobRivelata = o.job_rivelata === true || rivelata", source)
        self.assertIn("const corpo = jobRivelata ? testoCompleto : anteprima", source)
        self.assertIn("Company names, links and contacts remain hidden", source)
        self.assertIn("3 credits once · permanent access", source)
        self.assertIn("all company jobs", source)
        self.assertIn('class="app-btn app-btn-secondary">Reveal company · 3 credits</button>', source)
        self.assertIn("company-summary-card", source)
        self.assertIn("Open company profile", source)
        self.assertIn("Open original posting", source)
        self.assertIn("urlSicuro(o.url)", source)
        self.assertIn("event.stopPropagation()", source)
        self.assertIn('data-job-id="${esc(o.id)}"', source)
        self.assertIn("rivelaAzienda(${jsArg(o.reveal_token)}, this)", source)
        self.assertNotIn("The complete posting is locked", source)
        self.assertNotIn("bg-slate-950", source)
        self.assertNotIn("from-indigo-600", source)

    def test_job_unlock_retries_refreshes_in_place_and_restores_button(self):
        source = section("async function rivelaJob(", "async function rivelaAzienda(")
        self.assertIn("if (btn.disabled) return", source)
        self.assertEqual(source.count('api("/portale/rivela"'), 2)
        self.assertEqual(source.count("JSON.stringify({ id })"), 2)
        self.assertIn("r.status === 503", source)
        self.assertIn("r.status === 429", source)
        self.assertIn("r.status === 404", source)
        self.assertIn("You need 1 credit", source)
        self.assertIn("const seq = ++dettaglioSeq", source)
        self.assertIn("if (seq === dettaglioSeq) await dettaglioJob(id)", source)
        self.assertLess(source.index("aggiornaSaldoDopoRivelazione(d.saldo)"), source.index("await dettaglioJob(id)"))
        self.assertIn("btn.disabled = false", source)
        self.assertIn('btn.textContent = "Unlock full job · 1 credit"', source)
        self.assertIn('e.message !== "401"', source)
        self.assertNotIn("location.reload", source)

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
