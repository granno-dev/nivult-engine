"""Il framework per i browser headless: le piattaforme che richiedono rendering JavaScript.

Sei piattaforme priorità 2 sono SPA client-side: Personio, SuccessFactors,
iCIMS, Zoho Recruit, Softgarden, WeRecruit. Nessuna ha un'API JSON pubblica
— i dati vengono caricati via JavaScript dopo il caricamento della pagina.

Questo modulo fornisce il framework per gestirle tutte con un approccio
uniforme: Playwright apre la pagina, aspetta il rendering, e poi si leggono
i dati dal DOM o intercettando le chiamate API che il JavaScript fa.

REQUISITO: playwright installato (pip install playwright && playwright install chromium)

    python -m nivult.ats.headless --test personio --slug edding-group
    python -m nivult.ats.headless --scrape personio
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger("nivult.ats.headless")

# I pattern per estrarre le offerte dal DOM di ogni piattaforma.
# Ogni piattaforma ha la sua struttura HTML: qui definiamo COME riconoscere
# un'offerta e quali campi estrarre.
DOM_PATTERNS = {
    "personio": {
        "job_selector": "[class*='job'] a[href*='/job/']",
        "title_attr": "text",
        "url_attr": "href",
    },
    "zohorecruit": {
        "job_selector": "a[href*='Careers/'], [class*='job'] a",
        "title_attr": "text",
        "url_attr": "href",
    },
    "werecruit": {
        "job_selector": "a[href*='/offres/']",
        "title_attr": "text",
        "url_attr": "href",
    },
    # iCIMS e SuccessFactors: da investigare
    # Il loro JavaScript non espone le offerte in modo accessibile
    "icims": {
        "job_selector": "[class*='job'] a",
        "title_attr": "text",
        "url_attr": "href",
    },
    "successfactors": {
        "job_selector": "a, [role='link']",
        "title_attr": "text",
        "url_attr": "href",
    },
}
    "successfactors": {
        # SAP UI5: tabella con righe di offerte
        "job_selector": "[class*='jobRequisition'] a, .sapUiTableRow",
        "title_attr": "text",
        "url_attr": "href",
    },
    "icims": {
        # iCIMS: iframe con lista offerte (ma anche dirette)
        "job_selector": ".iCIMS_JobsTable a, [class*='jobTitle'] a",
        "title_attr": "text",
        "url_attr": "href",
    },
    "zohorecruit": {
        # Zoho: Vue.js, lista con classi zoho
        "job_selector": "[class*='job'] a, .careerJobOpening",
        "title_attr": "text",
        "url_attr": "href",
    },
    "softgarden": {
        "job_selector": "[class*='job'] a, .sg-job-item",
        "title_attr": "text",
        "url_attr": "href",
    },
    "werecruit": {
        "job_selector": "a[href*='/offres/']",
        "title_attr": "text",
        "url_attr": "href",
    },
}


@dataclass(slots=True)
class HeadlessJob:
    """Un'offerta estratta da una pagina renderizzata."""
    platform_id: str
    slug: str
    external_id: str
    title: str
    url: str
    location: str | None = None
    country: str | None = None
    city: str | None = None
    posted_at: datetime | None = None
    raw: dict | None = None


async def _scrape_pagina(page, url: str, platform_id: str) -> list[HeadlessJob]:
    """Apre una pagina e ne estrae le offerte tramite i selettori DOM."""
    pattern = DOM_PATTERNS.get(platform_id, {})
    selector = pattern.get("job_selector", "a[href*='job']")

    await page.goto(url, wait_until="networkidle")
    # Aspetta che il JavaScript renderizzi il contenuto
    await page.wait_for_timeout(3000)

    jobs = []
    elements = await page.query_selector_all(selector)
    for el in elements:
        try:
            title = (await el.text_content() or "").strip()
            href = await el.get_attribute("href") or ""
            if title and href:
                # Assicurati che l'URL sia assoluto
                if href.startswith("/"):
                    from urllib.parse import urljoin
                    href = urljoin(url, href)
                jobs.append(HeadlessJob(
                    platform_id=platform_id,
                    slug="",  # da impostare dal chiamante
                    external_id=href.rstrip("/").split("/")[-1],
                    title=title[:200],
                    url=href))
        except Exception:
            continue

    return jobs


async def _intercetta_api(page, platform_id: str) -> list[dict]:
    """Intercetta le chiamate API che il JavaScript fa durante il rendering.

    Molte SPA caricano i dati da un endpoint API dopo il rendering iniziale.
    Invece di parsare il DOM, intercettiamo la risposta API.
    """
    risposte: list[dict] = []

    async def handle_response(response):
        url = response.url
        content_type = response.headers.get("content-type", "")
        if "json" in content_type and any(
                kw in url.lower() for kw in ("job", "career", "vacancy", "position")):
            try:
                data = await response.json()
                risposte.append({"url": url, "data": data})
            except Exception:
                pass

    page.on("response", handle_response)
    return risposte


async def scrape_azienda(platform_id: str, slug: str, url: str) -> list[HeadlessJob]:
    """Scrape di UNA azienda su UNA piattaforma headless.

    Ritorna le offerte trovate. Usa Playwright (browser Chromium headless).
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        # Intercetta le chiamate API durante il caricamento
        # (più affidabile del DOM quando disponibile)
        api_data = await _intercetta_api(page, platform_id)

        # Fallback: estrai dal DOM
        jobs = await _scrape_pagina(page, url, platform_id)

        # Se l'intercettazione API ha trovato dati, usali
        if api_data:
            log.info("  API intercettata: %d risposte", len(api_data))
            # Da implementare per piattaforma specifica

        await browser.close()
        return jobs


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.headless", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", help="piattaforma da testare (personio, icims, …)")
    ap.add_argument("--slug", help="slug dell'azienda da testare")
    ap.add_argument("--url", help="URL completo della pagina da testare")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO)

    if args.test and (args.url or args.slug):
        url = args.url or f"https://{args.slug}.jobs.personio.com"
        if args.test == "werecruit":
            url = args.url or f"https://careers.werecruit.io/fr/{args.slug}"
        print(f"Test: {args.test} → {url}")
        jobs = asyncio.run(scrape_azienda(args.test, args.slug or "", url))
        print(f"Trovate {len(jobs)} offerte")
        for j in jobs[:5]:
            print(f"  {j.title} → {j.url}")
    else:
        print("Uso: --test <piattaforma> --slug <azienda> oppure --url <url>")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
