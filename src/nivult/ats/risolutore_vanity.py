"""Risolutore di domini vanity: `careers.azienda.com` -> tenant ATS reale.

Molte aziende ospitano il loro ATS dietro un dominio proprio
(`careers.azienda.com`, `jobs.azienda.it`): `company_domains` rileva la
PIATTAFORMA ma non lo SLUG del tenant, quindi i nostri adapter a slug non
raggiungono quelle offerte. Qui apriamo la career page, estraiamo lo slug
candidato con una regex per-ATS, e lo **verifichiamo** contro l'adapter
reale: solo i tenant che restituiscono offerte vere entrano in
`ats_companies`. Nessun indovinello, niente slug morti — la verifica e' la
garanzia.

E' il pezzo che separa "gli slug puliti trovati su Common Crawl" da "ogni
datore che ha un career site, comunque sia ospitato".
"""
from __future__ import annotations

import logging
import re

import httpx
import psycopg

from nivult.ats.adapters import ADAPTERS

log = logging.getLogger("nivult.ats.vanity")

_UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"

# Parole che la regex puo' catturare ma NON sono tenant (host d'infrastruttura,
# widget, path generici). La verifica le scarterebbe comunque, ma saltarle
# qui risparmia una fetch inutile all'adapter.
_STOP = {
    "www", "api", "app", "apps", "careers", "career", "jobs", "job", "static",
    "assets", "embed", "widget", "job-widget", "jobwidget", "certificate",
    "matomo", "cdn", "media", "images", "img", "fonts", "policy", "privacy",
    "terms", "cookie", "cookies", "analytics", "careers-analytics", "help",
    "support", "status", "blog", "about", "login", "auth", "account",
}

# Regex per-ATS: ogni gruppo e' un candidato slug del tenant, cercato
# nell'HTML della career page (script/iframe/API annidati) e nell'URL finale.
_RES: dict[str, re.Pattern] = {
    "lever": re.compile(
        r"(?:jobs\.lever\.co|api\.lever\.co/v0/postings)/"
        r"([a-zA-Z0-9][a-zA-Z0-9._-]+)"),
    "greenhouse": re.compile(
        r"(?:boards|job-boards)\.greenhouse\.io/(?!embed\b)"
        r"([a-z0-9]+)|job_board\?for=([a-z0-9]+)"),
    "recruitee": re.compile(r"([a-z0-9][a-z0-9-]+)\.recruitee\.com"),
    "personio": re.compile(
        r"([a-z0-9][a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    "workable": re.compile(
        r"apply\.workable\.com/(?:api/v\d+/widget/accounts/)?"
        r"([a-z0-9][a-z0-9-]+)|([a-z0-9][a-z0-9-]+)\.workable\.com"),
    "smartrecruiters": re.compile(
        r"careers\.smartrecruiters\.com/([A-Za-z0-9][A-Za-z0-9-]+)"
        r"|smartrecruiters\.com/v1/companies/([A-Za-z0-9][A-Za-z0-9-]+)"),
    "softgarden": re.compile(r"([a-z0-9][a-z0-9-]+)\.softgarden\.io"),
    "teamtailor": re.compile(
        r"([a-z0-9][a-z0-9-]+)\.teamtailor\.com"
        r"|career\.teamtailor\.com/c/([a-z0-9-]+)"),
    "ashby": re.compile(
        r"jobs\.ashbyhq\.com/([a-zA-Z0-9][a-zA-Z0-9._-]+)"),
}


def _candidato(platform_id: str, html_txt: str, url_finale: str) -> str | None:
    rx = _RES.get(platform_id)
    if not rx:
        return None
    blob = html_txt + " " + (url_finale or "")
    for m in rx.finditer(blob):
        for g in m.groups():
            if g and g.lower() not in _STOP and len(g) >= 2:
                return g
    return None


def _verifica(platform_id: str, slug: str) -> int:
    """Numero di offerte che l'adapter reale trova per questo slug (0 se
    lo slug e' falso/morto). E' la garanzia: niente entra senza offerte."""
    cls = ADAPTERS.get(platform_id)
    if cls is None:
        return 0
    ad = cls()
    try:
        return len(ad.jobs(slug))
    except Exception as exc:                     # noqa: BLE001
        log.debug("vanity verifica %s/%s: %s", platform_id, slug, exc)
        return 0
    finally:
        try:
            ad.close()
        except Exception:                        # noqa: BLE001
            pass


def risolvi(dsn: str, limite: int = 200, solo_nuovi: bool = True) -> dict:
    """Risolve un lotto di domini vanity e inserisce i tenant verificati.

    Scorre `company_domains` con una `careers_url` e un `platform_id`
    gestito, apre la pagina, estrae lo slug, lo verifica e — se da'
    offerte — lo inserisce in `ats_companies` (che il demone poi scrapa).
    """
    stats = {"esaminati": 0, "candidati": 0, "verificati": 0,
             "nuovi": 0, "gia_noti": 0, "offerte_trovate": 0}
    gestiti = list(_RES.keys())
    cli = httpx.Client(timeout=15, follow_redirects=True,
                       headers={"User-Agent": _UA})
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """SELECT careers_url, platform_id, domain, company_name, country
                 FROM company_domains
                WHERE careers_url IS NOT NULL
                  AND platform_id = ANY(%s)
                ORDER BY random() LIMIT %s""",
            (gestiti, limite)).fetchall()

        for careers_url, pid, dom, nome, paese in rows:
            stats["esaminati"] += 1
            try:
                r = cli.get(careers_url)
            except httpx.HTTPError:
                continue
            if r.status_code != 200:
                continue
            slug = _candidato(pid, r.text, str(r.url))
            if not slug:
                continue
            stats["candidati"] += 1

            # gia' censito? (dedup senza fetch se solo_nuovi)
            noto = conn.execute(
                "SELECT 1 FROM ats_companies WHERE platform_id=%s AND slug=%s",
                (pid, slug)).fetchone()
            if noto:
                stats["gia_noti"] += 1
                continue

            n = _verifica(pid, slug)
            if n <= 0:
                continue
            stats["verificati"] += 1
            stats["offerte_trovate"] += n

            ins = conn.execute(
                """INSERT INTO ats_companies
                       (platform_id, slug, company_name, country, discovered_from)
                   VALUES (%s, %s, %s, %s, 'vanity')
                   ON CONFLICT (platform_id, slug) DO NOTHING""",
                (pid, slug, (nome or None), (paese or None))).rowcount
            if ins:
                stats["nuovi"] += 1
                log.info("vanity: %s -> %s/%s (%d offerte)",
                         dom, pid, slug, n)
    cli.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    import os
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.risolutore_vanity")
    ap.add_argument("--limite", type=int, default=200)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    esito = risolvi(dsn, args.limite)
    log.info("Vanity: %s", esito)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
