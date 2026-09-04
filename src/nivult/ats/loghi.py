"""Il logo dell'azienda, a livello tenant, per la board delle offerte.

La board (cruscotto e futura landing) mostra il logo dell'azienda accanto
all'offerta: crea fiducia. Alcuni ATS danno il logo per-offerta (breezy,
niceboard, taleez, softgarden...), altri solo sulla pagina board come
`og:image` (ashby, lever, workable). Qui lo consolidiamo in
`ats_companies.logo_url` da due fonti — una volta per azienda, poi in cache —
cosi' la query della board fa un solo join e non ricalcola nulla.
"""
from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg

log = logging.getLogger("nivult.ats.loghi")

_UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"

# Board dove l'og:image E' il logo dell'azienda (verificato). Fuori da qui
# l'og:image e' spesso uno screenshot (teamtailor) o un logo generico
# dell'ATS (personio): meglio il monogramma che un logo sbagliato.
_BOARD = {
    "ashby":           "https://jobs.ashbyhq.com/{s}",
    "lever":           "https://jobs.lever.co/{s}",
    "workable":        "https://apply.workable.com/{s}/",
    "smartrecruiters": "https://careers.smartrecruiters.com/{s}",
    "greenhouse":      "https://job-boards.greenhouse.io/{s}",
}
# per alcuni ATS il logo NON e' nell'og:image ma in un <img> dedicato:
# regex per-piattaforma che pesca l'URL del logo dall'HTML della board.
_LOGO_IMG = {
    "greenhouse": re.compile(
        r"(https?://[a-z0-9-]*recruiting\.cdn\.greenhouse\.io/"
        r"external_greenhouse[^\"'\s]+)", re.I),
}
# host da rifiutare: asset generici dell'ATS, non il logo dell'azienda.
_RIFIUTA = re.compile(
    r"screenshots\.|/cdn_assets/|assets\.cdn\.|/rebrand|placeholder|default"
    r"|sr-careersite", re.I)

_OG = re.compile(
    r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)'
    r'|<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image', re.I)


def da_offerte(dsn: str) -> int:
    """Copia il logo per-offerta al livello azienda, in blocco SQL."""
    with psycopg.connect(dsn, autocommit=True) as c:
        r = c.execute("""
            UPDATE ats_companies ac
               SET logo_url = sub.logo, logo_checked_at = now()
              FROM (SELECT DISTINCT ON (platform_id, slug)
                           platform_id, slug, raw->>'logo' AS logo
                      FROM ats_jobs
                     WHERE raw->>'logo' LIKE 'http%'
                       AND expired_at IS NULL
                     ORDER BY platform_id, slug, fetched_at DESC) sub
             WHERE ac.platform_id = sub.platform_id
               AND ac.slug = sub.slug
               AND ac.logo_url IS NULL""")
        return r.rowcount


def da_board(dsn: str, limite: int = 400) -> dict:
    """Legge l'og:image della board per i tenant senza logo (ashby/lever/
    workable), a lotti. Solo loghi validi entrano."""
    cli = httpx.Client(timeout=12, follow_redirects=True,
                       headers={"User-Agent": _UA})
    stats = {"esaminati": 0, "trovati": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT platform_id, slug FROM ats_companies
             WHERE platform_id = ANY(%s) AND job_count > 0
               AND logo_url IS NULL
               AND (logo_checked_at IS NULL
                    OR logo_checked_at < now() - interval '30 days')
             ORDER BY logo_checked_at ASC NULLS FIRST
             LIMIT %s""", (list(_BOARD), limite)).fetchall()

        def _uno(row):
            pid, slug = row
            url = _BOARD[pid].format(s=slug)
            try:
                r = cli.get(url)
            except httpx.HTTPError:
                return pid, slug, None
            if r.status_code != 200:
                return pid, slug, None
            rx = _LOGO_IMG.get(pid)
            if rx is not None:
                mm = rx.search(r.text)
                logo = mm.group(1) if mm else None
            else:
                m = _OG.search(r.text)
                logo = (m.group(1) or m.group(2)) if m else None
            if logo and (_RIFIUTA.search(logo) or not logo.startswith("http")):
                logo = None
            return pid, slug, logo

        with ThreadPoolExecutor(max_workers=12) as ex:
            for pid, slug, logo in ex.map(_uno, righe):
                stats["esaminati"] += 1
                c.execute(
                    "UPDATE ats_companies SET logo_url=%s, logo_checked_at=now() "
                    "WHERE platform_id=%s AND slug=%s", (logo, pid, slug))
                if logo:
                    stats["trovati"] += 1
    cli.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.loghi")
    ap.add_argument("--da-offerte", action="store_true")
    ap.add_argument("--da-board", action="store_true")
    ap.add_argument("--limite", type=int, default=400)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.da_offerte or not (args.da_offerte or args.da_board):
        n = da_offerte(dsn)
        log.info("loghi da offerte: %d aziende aggiornate", n)
    if args.da_board:
        log.info("loghi da board: %s", da_board(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
