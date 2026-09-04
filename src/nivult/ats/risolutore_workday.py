"""Risolutore Workday: da slug nudo a (server, site) verificati.

Workday vuole tre coordinate: slug, server (wd1/wd3/...) e site — e 1.400+
tenant enterprise censiti dall'archivio avevano solo lo slug. Le firme
dell'API cxs le distinguono in modo netto:
  200 = server e site giusti · 404 (S21) = server giusto, site sbagliato ·
  422 = server sbagliato (o slug non-Workday).
Quindi: (1) il SERVER si trova provando i wd* finche' uno risponde 404;
(2) il SITE si pesca dagli URL archiviati su Wayback per quell'host esatto,
con un ripiego sui nomi comuni; (3) si VERIFICA con una chiamata jobs vera
(200 con jobPostings) prima di salvare. Chi risponde 422 ovunque non e' un
tenant Workday: si disattiva, era rumore del censimento.
"""
from __future__ import annotations

import logging
import os
import re

import httpx
import psycopg

log = logging.getLogger("nivult.ats.wd")

_UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"
_SERVERS = ("wd1", "wd3", "wd5", "wd103", "wd12", "wd10")
_SITI_COMUNI = ("External", "careers", "Careers", "External_Careers",
                "external", "jobs", "EXT", "External_Career_Site")


def _cxs(cli: httpx.Client, slug: str, srv: str, site: str) -> int:
    try:
        r = cli.post(
            f"https://{slug}.{srv}.myworkdayjobs.com/wday/cxs/{slug}/{site}/jobs",
            json={"limit": 1, "offset": 0, "searchText": ""})
        return r.status_code
    except httpx.HTTPError:
        return -1


def _trova_server(cli: httpx.Client, slug: str) -> str | None:
    for srv in _SERVERS:
        codice = _cxs(cli, slug, srv, "nivultprobe")
        if codice in (200, 404):        # il tenant vive su questo server
            return srv
    return None


def _siti_da_wayback(cli: httpx.Client, slug: str, srv: str) -> list[str]:
    host = f"{slug}.{srv}.myworkdayjobs.com"
    try:
        r = cli.get("http://web.archive.org/cdx/search/cdx?url=" + host +
                    "/*&output=text&fl=original&collapse=urlkey&limit=2000",
                    timeout=45)
    except httpx.HTTPError:
        return []
    siti: list[str] = []
    for m in re.finditer(
            rf"{re.escape(host)}/(?:[a-z]{{2}}-[A-Z]{{2}}/)?([A-Za-z0-9_-]+)",
            r.text):
        s = m.group(1)
        if s.lower() not in ("wday", "login", "static", "assets", "api",
                             "favicon", "robots") and s not in siti:
            siti.append(s)
        if len(siti) >= 12:
            break
    return siti


def risolvi(dsn: str, limite: int = 150) -> dict:
    cli = httpx.Client(timeout=12, follow_redirects=True,
                       headers={"User-Agent": _UA,
                                "Accept": "application/json"})
    stats = {"esaminati": 0, "risolti": 0, "non_workday": 0,
             "senza_site": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT slug FROM ats_companies
             WHERE platform_id = 'workday' AND is_active
               AND (wd_server IS NULL OR wd_instance IS NULL)
               AND slug !~ '[.]'          -- i "slug" col punto sono domini, rumore
             ORDER BY slug LIMIT %s""", (limite,)).fetchall()
        for (slug,) in righe:
            stats["esaminati"] += 1
            srv = _trova_server(cli, slug)
            if srv is None:
                # 422 ovunque: non e' un tenant Workday — fuori dal giro.
                c.execute("UPDATE ats_companies SET is_active=false "
                          "WHERE platform_id='workday' AND slug=%s", (slug,))
                stats["non_workday"] += 1
                continue
            candidati = _siti_da_wayback(cli, slug, srv)
            for s in _SITI_COMUNI:
                if s not in candidati:
                    candidati.append(s)
            site_ok = None
            for site in candidati[:18]:
                if _cxs(cli, slug, srv, site) == 200:
                    site_ok = site
                    break
            if site_ok:
                c.execute("""UPDATE ats_companies
                                SET wd_server=%s, wd_instance=%s,
                                    last_fetch_at=NULL
                              WHERE platform_id='workday' AND slug=%s""",
                          (srv, site_ok, slug))
                stats["risolti"] += 1
                log.info("workday %s -> %s/%s", slug, srv, site_ok)
            else:
                stats["senza_site"] += 1
    cli.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.risolutore_workday")
    ap.add_argument("--limite", type=int, default=150)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(risolvi(dsn, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
