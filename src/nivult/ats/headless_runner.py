"""Il runner per le piattaforme headless: quelle che richiedono Playwright.

    python -m nivult.ats.headless_runner                    # tutte
    python -m nivult.ats.headless_runner --piattaforma personio

Le piattaforme headless sono SPA JavaScript-rendered che non hanno API JSON
pubblica: Personio, Zoho Recruit, WeRecruit, iCIMS. Per ognuna apro un browser
Chromium headless, aspetto il rendering e estraggo le offerte dal DOM.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timezone

import psycopg
from psycopg.rows import dict_row

from nivult.ats.adapters import AtsJob

log = logging.getLogger("nivult.ats.headless_runner")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

# Come costruire l'URL per ogni piattaforma headless
URL_BUILDERS = {
    "personio": lambda slug: f"https://{slug}.jobs.personio.com",
    "zohorecruit": lambda slug: f"https://{slug}.zohorecruit.eu/jobs/Careers",
    "werecruit": lambda slug: f"https://careers.werecruit.io/fr/{slug}",
    "icims": lambda slug: f"https://{slug}.icims.com/jobs/",
    "successfactors": lambda slug: f"https://career17.sapsf.com/career?company={slug}",
}

# I selettori DOM per estrarre le offerte da ogni piattaforma
SELECTORS = {
    "personio": "[class*='job'] a[href*='/job/']",
    "zohorecruit": "a[href*='Careers/'], [class*='job'] a",
    "werecruit": "a[href*='/offres/']",
    "icims": "a[href*='job'], [class*='jobTitle'] a",
    "successfactors": "a, [role='link']",
}


async def _estrai_icims(page) -> list[dict]:
    """iCIMS carica le offerte dentro un iframe (icims_content_iframe).

    La pagina esterna è solo branding: il contenuto vero arriva nel frame
    con URL `.../jobs/search` o `.../jobs/intro`. Se siamo sull'intro,
    clicco "view all open positions" dentro il frame e riprovo.
    """
    import json

    ESTRAI = """els => els.map(e => ({
        text: ((e.textContent || '').replace(/\\s+/g, ' ')
                .replace(/^Title\\s*/, '').trim()).slice(0, 200),
        href: e.href || ''
    })).filter(l => l.href && l.text && /\\/jobs\\/\\d+\\//.test(l.href))"""

    for _ in range(2):
        for frame in page.frames:
            if frame == page.main_frame or "icims.com/jobs" not in frame.url:
                continue
            links = await frame.eval_on_selector_all("a[href*='/jobs/']", ESTRAI)
            if links:
                return links
            # frame intro: entra nella search
            try:
                await frame.click("a:has-text('view all open positions')",
                                  timeout=4000)
                await page.wait_for_timeout(3000)
            except Exception:
                pass
        await page.wait_for_timeout(2000)
    return []


async def scrape_headless(dsn: str, piattaforma: str | None = None) -> dict:
    """Scrape le aziende sulle piattaforme headless con Playwright."""
    from playwright.async_api import async_playwright

    stats = {"aziende": 0, "offerte": 0, "nuove": 0}

    with psycopg.connect(dsn) as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            sql = """
                SELECT ac.slug, ac.platform_id
                FROM ats_companies ac
                JOIN ats_platforms ap ON ap.id = ac.platform_id
                WHERE ac.is_active AND ap.is_active
                  AND ap.api_type IN ('headless', 'headless_difficult')
                  AND ac.last_fetch_at IS NULL
            """
            params: list = []
            if piattaforma:
                sql += " AND ac.platform_id = %s"
                params.append(piattaforma)
            cur.execute(sql + " ORDER BY ac.platform_id, ac.slug", params)
            aziende = cur.fetchall()

    if not aziende:
        print("Nessuna azienda headless da processare.")
        return stats

    print(f"Scraping headless di {len(aziende)} aziende...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        for az in aziende:
            platform_id = az["platform_id"]
            slug = az["slug"]
            stats["aziende"] += 1

            url = URL_BUILDERS.get(platform_id, lambda s: f"https://{s}")(slug)
            selector = SELECTORS.get(platform_id, "a[href*='job']")

            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(3000)

                # Estrai i link alle offerte
                if platform_id == "icims":
                    links = await _estrai_icims(page)
                else:
                    links = await page.eval_on_selector_all(
                        selector,
                        """els => els.map(e => ({
                            text: (e.textContent || '').trim().slice(0, 200),
                            href: e.href || ''
                        })).filter(l => l.href && l.text)"""
                    )

                # Salva nel database
                jobs_trovati = len(links)
                stats["offerte"] += jobs_trovati

                if jobs_trovati > 0:
                    with psycopg.connect(dsn) as conn:
                        with conn.cursor() as cur:
                            for link in links:
                                if platform_id == "icims":
                                    # .../jobs/<id>/<slug>/job?in_iframe=1 → <id>
                                    m = re.search(r"/jobs/(\d+)/", link["href"])
                                    if not m:
                                        continue
                                    external_id = m.group(1)
                                else:
                                    external_id = link["href"].rstrip("/").split("/")[-1]
                                cur.execute("""
                                    INSERT INTO ats_jobs (platform_id, slug, external_id,
                                      title, url, raw)
                                    VALUES (%s, %s, %s, %s, %s, %s)
                                    ON CONFLICT (platform_id, external_id) DO UPDATE SET
                                      title = EXCLUDED.title, url = EXCLUDED.url,
                                      fetched_at = now()
                                    RETURNING (xmax = 0) AS is_new
                                """, (platform_id, slug, external_id, link["text"],
                                      link["href"],
                                      psycopg.types.json.Json(link)))
                                r = cur.fetchone()
                                if r and r[0]:
                                    stats["nuove"] += 1
                        conn.commit()

                # Aggiorna lo stato dell'azienda
                with psycopg.connect(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE ats_companies SET last_fetch_at = now(), "
                            "job_count = %s WHERE platform_id = %s AND slug = %s",
                            (jobs_trovati, platform_id, slug))
                    conn.commit()

                log.info("  %s/%s: %d offerte", platform_id, slug, jobs_trovati)

            except Exception as exc:
                log.warning("  %s/%s: errore: %s", platform_id, slug, str(exc)[:80])
                # Segna comunque come processata per non riprovarci subito
                with psycopg.connect(dsn) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE ats_companies SET last_fetch_at = now(), job_count = 0 "
                            "WHERE platform_id = %s AND slug = %s",
                            (platform_id, slug))
                    conn.commit()

        await browser.close()

    return stats


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.headless_runner",
                                 description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--piattaforma",
                    help="solo questa piattaforma (personio, zohorecruit, werecruit, icims)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                        stream=sys.stderr)
    dsn = ATS_DSN
    print(f"database ATS: {dsn}")

    stats = asyncio.run(scrape_headless(dsn, args.piattaforma))

    print(f"\nScrape headless: {stats['aziende']} aziende, "
          f"{stats['offerte']} offerte ({stats['nuove']} nuove)")

    # Statistiche finali
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT platform_id, count(*) FROM ats_jobs GROUP BY 1 ORDER BY 2 DESC")
            print("\nOfferte per piattaforma:")
            for platform, n in cur.fetchall():
                print(f"  {platform:<18} {n:>6}")
            cur.execute("SELECT count(*) FROM ats_jobs")
            print(f"\nTotale: {cur.fetchone()[0]} offerte")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
