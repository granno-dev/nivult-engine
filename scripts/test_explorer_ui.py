import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "vetrina" / "account.html").read_text()
TERMS = (ROOT / "vetrina" / "terms.html").read_text()


def section(start: str, end: str) -> str:
    return HTML[HTML.index(start):HTML.index(end, HTML.index(start))]


class ExplorerUiTest(unittest.TestCase):
    def test_nonblocking_right_drawer_shell_and_responsive_tokens(self):
        shell = section('<div id="dettaglio"', '<div id="export-modale"')
        self.assertIn('aria-modal="true"', shell)
        self.assertIn("explorer-drawer-backdrop", shell)
        self.assertIn('id="dettaglio-box" class="explorer-drawer"', shell)
        self.assertLess(HTML.index('id="vista-dash"'), HTML.index('id="dettaglio"'))
        self.assertLess(HTML.index('id="explorer-reference-panel"'), HTML.index('id="dettaglio"'))
        css = section(".explorer-lock-pill,", "@keyframes spin")
        self.assertIn("width: min(680px,58vw)", css)
        self.assertIn("height: 100vh", css)
        self.assertIn("transition: transform 200ms ease", css)
        self.assertIn("background: rgba(248,250,252,.58)", css)
        self.assertIn("@media (max-width: 767px)", css)
        self.assertIn("width: 100vw", css)
        for token in ["#F8FAFC", "#E2E8F0", "#0F172A", "#2563EB", "#1D4ED8", "#047857", "#ECFDF5", "#A7F3D0", "#3730A3", "#EEF2FF", "#C7D2FE", "#B45309", "#FFFBEB", "#FDE68A"]:
            self.assertIn(token, css)

    def test_sticky_header_has_four_tabs_and_one_vertical_scroll(self):
        header = section("function drawerHeader(", "function techPreview(")
        for label in ["This Job", "Company Profile", "Tech Stack", "Active Jobs (${active})"]:
            self.assertIn(label, header)
        self.assertIn("Preview · 0 credits", header)
        self.assertIn("Unlocked · Permanent", header)
        self.assertIn("Reveal company · 3 credits", HTML)
        css = section(".explorer-drawer-shell {", "@keyframes spin")
        self.assertIn("position: sticky", css)
        self.assertEqual(css.count("overflow-y: auto"), 1)
        drawer_code = section("function renderDrawer(", "function companyJobsContextValid(")
        self.assertNotIn("max-h-", drawer_code)
        self.assertNotIn("overflow-auto", drawer_code)

    def test_preview_is_structured_and_never_uses_description_fields(self):
        preview = section("function companySignals(", "function copiaDominio(")
        for marker in ["companySize(record)", "record.industry", "companyActiveJobs", "companyPosted30", "languageList", "techPreview(record)", "revealValueCard(record)"]:
            self.assertIn(marker, preview)
        self.assertIn("Aggregator", preview)
        self.assertNotIn("record.description", preview)
        self.assertNotIn("description_preview", HTML)
        self.assertIn("3 credits once · Permanent access to company profile, tech stack, and all ${count} postings", HTML)
        self.assertIn("record.source_label", HTML)
        self.assertIn('record.is_aggregator === true ? "Job aggregator" : "Direct Career Site"', HTML)
        explorer_ui = section("function sourceLabel(", "function _quando(").lower()
        for jargon in ["jsonld", "sanitized", "redacted"]:
            self.assertNotIn(jargon, explorer_ui)

    def test_only_permanent_company_reveal_exists(self):
        self.assertNotIn("rivelaJob", HTML)
        self.assertNotIn('api("/portale/rivela"', HTML)
        self.assertNotIn("Unlock full job", HTML)
        self.assertNotIn("1 credit once", HTML)
        self.assertIn('api("/portale/rivela-azienda"', HTML)
        self.assertIn("Company unlocked · Permanent access", HTML)
        self.assertIn("Unlocked ✓", HTML)
        self.assertIn("no separate single-job reveal", TERMS)
        self.assertNotIn("sanitized", TERMS)

    def test_reveal_is_in_place_guarded_and_refreshes_this_job(self):
        source = section("async function rivelaAzienda(", "let dettaglioCorrente")
        self.assertIn("const seq = ++dettaglioSeq", source)
        self.assertIn("r.status === 503", source)
        self.assertIn("aggiornaRigheRivelate(group)", source)
        self.assertIn("aggiornaSaldoDopoRivelazione(d.saldo)", source)
        self.assertIn('dettaglioJob(jobId, { revealRefresh: true })', source)
        self.assertIn('notifica("Company unlocked · Permanent access")', source)
        self.assertLess(source.index("aggiornaRigheRivelate(group)"), source.index("dettaglioJob(jobId"))
        detail = section("async function dettaglioJob(", "function languageList(")
        self.assertIn('options.revealRefresh ? "job"', detail)
        self.assertIn("if (seq !== dettaglioSeq) return", detail)

    def test_low_balance_disables_reveal_and_topup_closes_first(self):
        source = section("function revealButtonMarkup(", "function aziendaAssente(")
        self.assertIn("saldoDisponibile() < 3", source)
        self.assertIn('low ? "disabled"', source)
        self.assertIn("Need 3 credits to reveal", source)
        self.assertIn("Top up credits", source)
        close = source.index("chiudiDettaglio();vaiAiCrediti()")
        self.assertLess(source.index("chiudiDettaglio()", close), source.index("vaiAiCrediti()", close))
        topup = section("function vaiAiCrediti(", "function vaiAiPiani(")
        self.assertLess(topup.index("chiudiDettaglio(false)"), topup.index('location.hash = "#billing"'))

    def test_rows_have_static_locks_three_chips_and_quick_actions(self):
        rows = section("function quickRevealButton(", "function cambiaTab(")
        self.assertIn("Hidden company", rows)
        self.assertIn("slice(0, 3)", rows)
        self.assertIn("sourceBadge(o)", rows)
        self.assertIn("Reveal · 3cr", rows)
        self.assertIn("Unlocked ✓", rows)
        self.assertIn('role="button" tabindex="0"', rows)
        self.assertNotIn("blur-", rows)
        self.assertNotIn("skel", rows)
        self.assertIn("skel", section("function mostraCaricamento(", "async function cerca("))

    def test_keyboard_navigation_preserves_drawer_and_ignores_form_controls(self):
        source = section('document.addEventListener("keydown", event => {', "async function apriAziendaDaJob(")
        self.assertIn('["ArrowUp", "ArrowDown"]', source)
        self.assertIn('event.key === "Escape"', source)
        self.assertIn("input, textarea, select", source)
        self.assertIn('document.querySelectorAll("#cerca-righe > [data-job-id]")', source)
        self.assertIn("row.getClientRects().length > 0", source)
        self.assertNotIn('document.querySelectorAll("#cerca-righe [data-job-id]")', source)
        self.assertIn("dettaglioJob(rows[next].dataset.jobId)", source)
        self.assertIn('["Enter", " "]', source)
        self.assertIn("chiudiDettaglio()", source)
        self.assertIn("if (!dettaglioVisibile() || dialogoAttivo) return", source)

    def test_drawer_refresh_and_row_updates_restore_focus(self):
        source = section("function apriDettaglio(", "function esc(")
        self.assertNotIn("apriDialogo", source)
        self.assertIn("const wasOpen = dettaglioVisibile()", source)
        self.assertIn("focusState", source)
        self.assertIn("focusPrimaDelDettaglio", source)
        self.assertIn("focusDettaglioFallback", source)
        self.assertIn("target?.focus({ preventScroll: true })", source)
        rows = section("function aggiornaRigheRivelate(", "function quickRevealButton(")
        self.assertIn("activeRow", rows)
        self.assertIn("restored?.focus({ preventScroll: true })", rows)

    def test_company_profile_aliases_badges_about_and_location_fallback(self):
        self.assertIn("record.name || record.company", HTML)
        self.assertIn("record.company_size || record.company_size_band", HTML)
        self.assertIn("record.about || record.description", HTML)
        self.assertIn('["locations_count", "n_locations"]', HTML)
        self.assertIn('["active_postings_count", "active_jobs"]', HTML)
        self.assertIn('["posted_30d_count", "jobs_posted_30d"]', HTML)
        profile = section("function companyProfilePanel(", "function companyTechPanel(")
        self.assertIn("locationCount > 0", profile)
        self.assertIn('[a.country, "Country"]', profile)
        self.assertIn("languageList(a)", profile)
        self.assertIn("companyAbout(a)", profile)
        self.assertIn("logoRivelato(a", profile)

    def test_grouped_tech_and_active_jobs_filter_first_fifteen(self):
        tech = section("function companyTechPanel(", "function companyJobsListMarkup(")
        self.assertLess(tech.index("Core &amp; Vertical Tech"), tech.index("Office &amp; Productivity"))
        jobs = section("function companyJobsListMarkup(", "function companyJobsContextValid(")
        self.assertIn("filtered.slice(0, 15)", jobs)
        self.assertIn("Show all ${filtered.length}", jobs)
        self.assertIn("Filter ${total} postings...", jobs)
        self.assertIn("dettaglioJob(${jsArg(job.id)})", jobs)
        loader = section("async function caricaAziendaJobs(", "async function mostraAzienda(")
        self.assertIn(".slice(0, 50)", loader)

    def test_expired_company_jobs_refresh_uses_full_scoped_company_ref(self):
        loader = section("async function caricaAziendaJobs(", "async function mostraAzienda(")
        self.assertIn("r.status === 400", loader)
        self.assertIn("await apriAziendaDaJob(revealToken)", loader)
        self.assertIn("context.companyJobsLoading = false", loader)
        self.assertIn("context.companyJobsError = \"unavailable\"", loader)
        registration = section("async function dettaglioJob(", "function languageList(")
        self.assertIn("d.company_ref || offerta.company_ref", registration)
        cache = section("function registraRivelazione(", "function iconaAziendaNascosta(")
        self.assertIn("company_ref: merged.company_ref", cache)
        renewal = section("async function apriAziendaDaJob(", "function companyProfilePanel(")
        self.assertIn("cached.company_ref", renewal)
        self.assertIn("cached.ats}:${cached.company_slug}", renewal)

    def test_company_jobs_stale_check_uses_sequence_company_and_token(self):
        source = section("function companyJobsContextValid(", "async function mostraAzienda(")
        self.assertIn("jobsSeq === aziendaJobsSeq", source)
        self.assertIn("dettaglioCorrente.revealToken === revealToken", source)
        self.assertIn("dettaglioCorrente.companyKey === companyKey", source)
        self.assertGreaterEqual(source.count("companyJobsContextValid(jobsSeq, revealToken, companyKey)"), 3)

    def test_logo_fallback_escaping_and_http_source_validation_survive(self):
        logo = section("function logoRivelato(", "function aggiornaRigheRivelate(")
        self.assertLess(logo.index("const local"), logo.index("const remote"))
        self.assertIn("company-logo-monogram", logo)
        self.assertIn("next < sources.length", logo)
        self.assertNotIn("onerror=", logo)
        self.assertNotIn("img.remove", logo)
        safe_url = section("function urlSicuro(", "function drawerLoading(")
        self.assertIn('["http:", "https:"]', safe_url)
        full = section("function jobFull(", "function lockedCompanyPanel(")
        self.assertIn("urlSicuro(record.url) || urlSicuro(record.source_url)", full)
        self.assertIn("Open original posting ↗", full)
        self.assertIn("Copy domain", full)
        self.assertIn("Copy API cURL", full)
        self.assertIn("esc(description)", full)
        self.assertIn("esc(original)", full)

    def test_account_cleanup_title_cleanup_and_backend_routes_survive(self):
        logout = section("async function esci(", "let dialogoAttivo")
        self.assertIn('credentials: "include"', logout)
        self.assertEqual(HTML.count('credentials: "include"'), 1)
        self.assertIn("finally", logout)
        self.assertIn("pulisciStatoAccount()", logout)
        self.assertIn("mostraLogin()", logout)
        self.assertIn("window.location.replace('/account')", logout)
        self.assertIn("window.addEventListener('pageshow'", HTML)
        self.assertIn('u.searchParams.delete("token")', HTML)
        for route in ["/portale/offerta-dettaglio", "/portale/rivela-azienda", "/portale/azienda-jobs", "/portale/token-azienda"]:
            self.assertIn(route, HTML)
        self.assertIn("chiudiDettaglio(false)", section("function route()", "function apriDrawer("))


if __name__ == "__main__":
    unittest.main()
