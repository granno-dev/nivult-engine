"""La scoperta massiva: mappa TUTTE le aziende europee su OGNI piattaforma ATS.

Due fonti, combinate:

  Common Crawl    l'indice pubblico del web (gratis) — per ogni piattaforma
                  ATS, cerca tutti gli URL che matchano il pattern ed estrae
                  lo slug dell'azienda. Trova le aziende che NON conosciamo.

  DB produzione   gli URL delle offerte che Fantastic ci ha già dato —
                  una fonte già verificata, con aziende che sappiamo essere
                  attive (sola lettura, nessuna interferenza).

Il risultato è l'INDICE: non le offerte, ma la MAPPA di chi usa quale ATS.
Le offerte si scaricano dopo, on-demand, per i cluster che servono.

    python -m nivult.ats.discovery --common-crawl     # scoperta Common Crawl
    python -m nivult.ats.discovery --from-production  # estrazione dal DB prod
    python -m nivult.ats.discovery --stats            # stato dell'indice
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

import httpx
import psycopg
from psycopg.rows import dict_row

from nivult.ats.registry import tutte_le_piattaforme, per_priorita

log = logging.getLogger("nivult.ats.discovery")

ATS_DSN = os.environ.get(
    "ATS_DATABASE_URL",
    "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")

# L'indice Common Crawl da interrogare (il più recente che risponde)
CC_INDEX = "CC-MAIN-2026-30"


def _cc_client() -> httpx.Client:
    return httpx.Client(
        timeout=60,
        headers={"User-Agent": "nivult-ats/0.1 (research; https://nivult.com)"})


def cc_scopri_piattaforma(piattaforma: dict, limite: int = 5000) -> set[str]:
    """Scopre le aziende su UNA piattaforma via Common Crawl.

    Interroga l'indice CDX di Common Crawl con il pattern di ricerca della
    piattaforma, legge gli URL restituiti, estrae lo slug dell'azienda.
    Ritorna un set di slug.
    """
    pattern = piattaforma.get("cc_search")
    if not pattern:
        return set()

    url_regex = piattaforma.get("url_pattern", "")
    slug_set: set[str] = set()
    offset = 0
    page_size = 500  # Common Crawl paginazione

    with _cc_client() as client:
        while len(slug_set) < limite:
            api_url = (
                f"https://index.commoncrawl.org/{CC_INDEX}-index"
                f"?url={pattern}&output=json&limit={page_size}&offset={offset}")

            try:
                r = client.get(api_url)
                if r.status_code != 200:
                    log.warning("  CC risponde %d per %s, fermo",
                                r.status_code, piattaforma["id"])
                    break
                # CDX restituisce una riga JSON per linea
                righe = [l for l in r.text.split("\n") if l.strip()]
                if not righe:
                    break

                trovate_in_pagina = 0
                for riga in righe:
                    try:
                        d = json.loads(riga)
                        url = d.get("url", "")
                        # Estrai lo slug con il regex della piattaforma
                        if url_regex:
                            m = re.search(url_regex, url)
                            if m and m.lastindex:
                                slug = m.group(1)
                                if slug and len(slug) > 1 and "/" not in slug:
                                    slug_set.add(slug)
                                    trovate_in_pagina += 1
                    except json.JSONDecodeError:
                        continue

                if trovate_in_pagina == 0:
                    break
                offset += page_size
                # Rate limit cortese verso Common Crawl
                time.sleep(1)

            except httpx.TimeoutException:
                log.warning("  CC timeout per %s a offset %d",
                            piattaforma["id"], offset)
                break
            except Exception as exc:  # noqa: BLE001
                log.warning("  CC errore per %s: %s", piattaforma["id"], exc)
                break

    return slug_set


def cc_scopri_tutte(dsn: str, priorita_max: int = 3, limite: int = 5000) -> dict:
    """Scopre le aziende su TUTTE le piattaforme via Common Crawl.

    priorita_max: 1 = solo le critiche, 2 = anche le importanti, 3 = tutte.
    """
    stats: dict[str, int] = {}

    for piattaforma in tutte_le_piattaforme():
        if piattaforma["priority"] > priorita_max:
            continue

        pid = piattaforma["id"]
        log.info("Common Crawl → %s (pattern: %s)", pid, piattaforma.get("cc_search"))

        slug_set = cc_scopri_piattaforma(piattaforma, limite)
        stats[pid] = len(slug_set)

        if slug_set:
            # Salva nel database
            with psycopg.connect(dsn) as conn:
                with conn.cursor() as cur:
                    for slug in slug_set:
                        cur.execute(
                            "INSERT INTO ats_companies (platform_id, slug, "
                            "discovered_from) VALUES (%s, %s, 'common_crawl') "
                            "ON CONFLICT (platform_id, slug) DO NOTHING",
                            (pid, slug))
                conn.commit()

        log.info("  %s: %d aziende scoperte", pid, len(slug_set))

    return stats


def da_produzione(dsn: str, dsn_produzione: str) -> dict:
    """Estrae TUTTE le aziende da OGNI piattaforma dagli URL nel DB produzione.

    Per ogni piattaforma nel registro, cerca gli URL nel DB che matchano il
    pattern e ne estrae lo slug. È la fonte più affidabile perché le aziende
    sono già state verificate da Fantastic.
    """
    stats: dict[str, int] = {}

    for piattaforma in tutte_le_piattaforme():
        pid = piattaforma["id"]
        url_regex = piattaforma.get("url_pattern", "")
        if not url_regex:
            continue

        # Estrai le aziende dal DB di produzione
        try:
            with psycopg.connect(dsn_produzione) as prod:
                with prod.cursor() as cur:
                    cur.execute(
                        f"SELECT DISTINCT substring(url from '{url_regex}') "
                        f"FROM jobs WHERE url ~ '{url_regex}' "
                        f"AND status = 'active'")
                    slug_set = {r[0] for r in cur.fetchall()
                                if r[0] and len(r[0]) > 1 and "/" not in r[0]}
        except psycopg.Error as exc:
            log.warning("  Errore produzione per %s: %s", pid, exc)
            continue

        stats[pid] = len(slug_set)

        if slug_set:
            with psycopg.connect(dsn) as ats:
                with ats.cursor() as cur:
                    for slug in slug_set:
                        cur.execute(
                            "INSERT INTO ats_companies (platform_id, slug, "
                            "discovered_from) VALUES (%s, %s, 'existing_db') "
                            "ON CONFLICT (platform_id, slug) DO NOTHING",
                            (pid, slug))
                ats.commit()

        log.info("  %s: %d aziende dal DB produzione", pid, len(slug_set))

    return stats




def dns_scopri(dsn: str) -> dict:
    """Scopre le aziende cercando i sottodomini DNS di ogni piattaforma ATS.

    Per ogni piattaforma che usa {slug}.{dominio} (es. teamtailor.com),
    interroga Hackertarget per trovare TUTTI i sottodomini esistenti.
    Ogni sottodominio è un'azienda con un career page su quella piattaforma.

    GRATIS: 50 risultati per query (free tier di Hackertarget).
    Più efficiente di Common Crawl: diretto, veloce, sempre aggiornato.
    """
    import httpx

    # Le piattaforme che usano sottodomini per le aziende
    PIATTAFORME_DNS = [
        ("teamtailor", "teamtailor.com", ".teamtailor.com"),
        ("personio", "jobs.personio.com", ".jobs.personio.com"),
        ("recruitee", "recruitee.com", ".recruitee.com"),
        ("breezy", "breezy.hr", ".breezy.hr"),
        ("homerun", "homerun.co", ".homerun.co"),
        ("icims", "icims.com", ".icims.com"),
        ("zohorecruit", "zohorecruit.eu", ".zohorecruit.eu"),
        ("workable", "workable.com", ".workable.com"),
        ("bamboohr", "bamboohr.com", ".bamboohr.com"),
        ("pinpoint", "pinpointhq.com", ".pinpointhq.com"),
        ("join", "join.com", ".join.com"),
        ("ashby", "ashbyhq.com", ".ashbyhq.com"),
    ]

    # Domini di infrastruttura da escludere (non sono aziende)
    ESCLUDI = {
        "www", "api", "app", "admin", "blog", "docs", "help", "support",
        "status", "cdn", "assets", "static", "mail", "smtp", "ftp",
        "staging", "dev", "test", "demo", "careers", "jobs", "portal",
        "login", "auth", "sso", "oauth", "redirect",
    }

    stats: dict[str, int] = {}
    with httpx.Client(timeout=30) as client:
        for platform_id, dominio, suffix in PIATTAFORME_DNS:
            try:
                r = client.get(
                    f"https://api.hackertarget.com/hostsearch/?q={dominio}")
                if r.status_code != 200 or not r.text.strip():
                    continue

                subdomains = set()
                for line in r.text.strip().split("
"):
                    if "," in line:
                        sub = line.split(",")[0].lower().strip()
                        if sub.endswith(suffix) and sub != dominio:
                            slug = sub[:-len(suffix)]
                            if slug and "." not in slug and slug not in ESCLUDI:
                                subdomains.add(slug)

                if subdomains:
                    stats[platform_id] = len(subdomains)
                    with psycopg.connect(dsn) as conn:
                        with conn.cursor() as cur:
                            for slug in subdomains:
                                cur.execute(
                                    "INSERT INTO ats_companies (platform_id, slug, "
                                    "discovered_from) VALUES (%s, %s, 'dns_enumeration') "
                                    "ON CONFLICT (platform_id, slug) DO NOTHING",
                                    (platform_id, slug))
                        conn.commit()
                    log.info("  %s: %d aziende via DNS", platform_id, len(subdomains))

            except Exception as exc:
                log.warning("  %s: errore DNS: %s", platform_id, exc)

    return stats


def stats(dsn: str) -> None:
    """Lo stato dell'indice: quante aziende per piattaforma, da dove."""
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ap.id, ap.name, ap.priority, ap.api_type,
                       count(ac.id) AS aziende,
                       count(ac.id) FILTER (
                         WHERE ac.discovered_from = 'common_crawl'
                       ) AS da_cc,
                       count(ac.id) FILTER (
                         WHERE ac.discovered_from = 'existing_db'
                       ) AS da_prod
                FROM ats_platforms ap
                LEFT JOIN ats_companies ac ON ac.platform_id = ap.id
                GROUP BY ap.id, ap.name, ap.priority, ap.api_type
                ORDER BY ap.priority, count(ac.id) DESC
            """)
            print(f"\n{'Piattaforma':<28} {'Pri':>3} {'API':<10} "
                  f"{'Aziende':>8} {'CC':>6} {'Prod':>6}")
            print("─" * 75)
            totale = 0
            for pid, nome, prio, api, n, cc, prod in cur.fetchall():
                totale += n
                print(f"{nome:<28} {prio:>3} {api:<10} {n:>8} {cc:>6} {prod:>6}")
            print("─" * 75)
            print(f"{'TOTALE':<28} {'':>3} {'':<10} {totale:>8}")

            cur.execute("SELECT count(*) FROM ats_jobs")
            print(f"\nOfferte già scaricate: {cur.fetchone()[0]}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nivult.ats.discovery", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--common-crawl", action="store_true",
                    help="scopri aziende via Common Crawl (gratis)")
    ap.add_argument("--dns", action="store_true",
                    help="scopri aziende via DNS enumeration (Hackertarget)")
    ap.add_argument("--from-production", action="store_true",
                    help="estrai aziende dal DB di produzione (sola lettura)")
    ap.add_argument("--priorita", type=int, default=3, choices=[1, 2, 3],
                    help="fino a quale priorità andare (default: tutte)")
    ap.add_argument("--limite", type=int, default=5000,
                    help="massimo aziende per piattaforma (default: 5000)")
    ap.add_argument("--stats", action="store_true", help="solo statistiche")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s",
                        stream=sys.stderr)
    dsn = ATS_DSN

    if args.common_crawl:
        s = cc_scopri_tutte(dsn, args.priorita, args.limite)
        print(f"\nCommon Crawl: {sum(s.values())} aziende totali scoperte")

    if args.dns:
        s = dns_scopri(dsn)
        print(f"\nDNS: {sum(s.values())} aziende totali scoperte")

    if args.from_production:
        dsn_prod = os.environ.get(
            "DATABASE_URL",
            "postgresql://giusepperanno@127.0.0.1:5432/nivult_dev")
        s = da_produzione(dsn, dsn_prod)
        print(f"\nDB produzione: {sum(s.values())} aziende estratte")

    stats(dsn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
