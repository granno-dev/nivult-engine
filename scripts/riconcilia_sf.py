#!/usr/bin/env python3
"""Riconcilia i tenant SuccessFactors registrati con lo slug sbagliato.

Il 07/09/2026, 1.029 tenant SuccessFactors su 1.107 erano a zero. Su
400 verificati a mano: 88 erano siti SuccessFactors veri che l'adapter
non sapeva leggere (riparato), 312 NON erano siti carriere — l'impronta
«successfactors» era scattata sul link al login dei dipendenti e lo
slug salvato era la homepage (crh.com, also.com), che nessun adapter
puo' leggere.

Per ogni tenant a zero si cerca l'host carriere VERO con lo stesso
risolutore del detector (`_host_sf`): l'host dello slug stesso, la
`careers_url` che il detector aveva annotato per quel dominio, i
sottodomini jobs./careers./karriere. citati in homepage.

  - host verificato = slug          → resta cosi' (l'adapter nuovo lo legge)
  - host verificato diverso         → nuovo tenant con quello slug,
                                      il vecchio si disattiva
  - nessun host SuccessFactors      → il tenant si disattiva e il dominio
                                      torna «pending» in company_domains,
                                      cosi' il ripasso lo riclassifica con
                                      il detector corretto

    ATS_DATABASE_URL=... python scripts/riconcilia_sf.py --dry-run
    ATS_DATABASE_URL=... python scripts/riconcilia_sf.py --thread 16
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import httpx
import psycopg

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from nivult.ats.detector import _host_sf  # noqa: E402

log = logging.getLogger("riconcilia_sf")


def _esamina(riga: tuple) -> tuple:
    slug, careers_url = riga
    with httpx.Client(timeout=12, follow_redirects=True,
                      headers={"User-Agent": "nivult-ats/0.1"}) as cl:
        html = ""
        try:
            r = cl.get(f"https://{slug}/")
            if r.status_code == 200:
                html = r.text
        except httpx.HTTPError:
            pass
        # prima lo slug stesso come host carriere, poi la careers_url
        # annotata dal detector, poi i sottodomini citati in pagina
        host = _host_sf(cl, f"https://{slug}/", html) or (
            _host_sf(cl, careers_url, html) if careers_url else None)
    return slug, host


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--thread", type=int, default=16)
    ap.add_argument("--limite", type=int, default=5000)
    a = ap.parse_args()
    dsn = os.environ["ATS_DATABASE_URL"]
    with psycopg.connect(dsn) as conn:
        righe = conn.execute("""
            SELECT c.slug, d.careers_url
              FROM ats_companies c
              LEFT JOIN company_domains d ON d.domain = c.slug
             WHERE c.platform_id = 'successfactors' AND c.is_active AND c.job_count = 0
             ORDER BY c.slug LIMIT %s""", (a.limite,)).fetchall()
    log.info("tenant SuccessFactors a zero da riconciliare: %d", len(righe))

    stats = {"esaminati": 0, "stesso_host": 0, "rislugati": 0, "non_sf": 0}
    esiti: list[tuple] = []
    with ThreadPoolExecutor(max_workers=a.thread) as pool:
        futuri = [pool.submit(_esamina, r) for r in righe]
        for fut in as_completed(futuri):
            try:
                slug, host = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("errore: %s", exc)
                continue
            stats["esaminati"] += 1
            esiti.append((slug, host))
            if stats["esaminati"] % 100 == 0:
                log.info("  … %d", stats["esaminati"])

    with psycopg.connect(dsn) as conn:
        for slug, host in esiti:
            if host == slug:
                stats["stesso_host"] += 1
                continue
            if host:
                stats["rislugati"] += 1
                log.info("  %s → %s", slug, host)
                if not a.dry_run:
                    conn.execute("""
                        INSERT INTO ats_companies (platform_id, slug, company_name, country, discovered_from)
                        SELECT 'successfactors', %s, company_name, country, 'riconcilia'
                          FROM ats_companies WHERE platform_id = 'successfactors' AND slug = %s
                        ON CONFLICT (platform_id, slug) DO UPDATE SET is_active = true""", (host, slug))
                    conn.execute("UPDATE ats_companies SET is_active = false "
                                 "WHERE platform_id = 'successfactors' AND slug = %s", (slug,))
            else:
                stats["non_sf"] += 1
                if not a.dry_run:
                    conn.execute("UPDATE ats_companies SET is_active = false "
                                 "WHERE platform_id = 'successfactors' AND slug = %s", (slug,))
                    conn.execute("UPDATE company_domains SET status = 'pending', platform_id = NULL "
                                 "WHERE domain = %s AND status = 'ats' AND platform_id = 'successfactors'",
                                 (slug,))
        if a.dry_run:
            conn.rollback()
        else:
            conn.commit()
    print("Riconcilia SF:", stats, "(dry-run)" if a.dry_run else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
