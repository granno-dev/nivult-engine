"""Scoperta di tenant ATS dagli archivi pubblici di URL.

Il censimento per indovinello (nomi legali → domini) è morto: 86% di
domini inesistenti. Qui si fa il contrario — si parte da URL che sono
ESISTITI davvero, perché qualcuno li ha archiviati:

  * l'indice Common Crawl (index.commoncrawl.org), un crawl al mese;
  * il CDX della Wayback Machine, che ricorda vent'anni di sottodomini.

Per ogni piattaforma con `url_pattern` in ats_platforms si interroga
l'archivio sul dominio base e si applica il pattern agli URL restituiti:
ogni cattura è uno slug di tenant reale. La prova sul campo (2026-09-02):
intervieweb.it, 13 tenant a censimento → 187 trovati con una sola
passata parziale, e sono Autogrill, Eurospin, ENAV, BRT — non parcheggi.

La Wayback Machine è permalosa sul ritmo (429 dopo poche richieste
ravvicinate): qui si dorme tra le pagine e si tiene un segnalibro per
piattaforma in /var/lib/nivult, così ogni notte si riprende da dove si
era rimasti invece di ricominciare.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

import psycopg

from .runner import ATS_DSN

log = logging.getLogger("nivult.ats.scoperta_archivi")

SEGNALIBRI = "/var/lib/nivult/scoperta_archivi.json"
UA = "nivult-engine/1.0 (job discovery; hello@nivult.com)"

# Dove il dominio base non si ricava pulito dal pattern (regex con
# alternanze o classi), lo si dice a mano. Per tutti gli altri lo si
# deriva togliendo i gruppi di cattura.
DOMINI_A_MANO = {
    "workday": "myworkdayjobs.com",
    "greenhouse": "greenhouse.io",
    "werecruit": "werecruit.io",
    "inrecruiting": "intervieweb.it",
}

# Sottodomini che non sono mai tenant.
SLUG_SPAZZATURA = {
    "www", "app", "api", "cdn", "static", "assets", "help", "docs",
    "blog", "status", "mail", "smtp", "ftp", "admin", "login", "auth",
    "developer", "developers", "support", "careers", "jobs", "demo",
    "sandbox", "staging", "test", "dev", "development", "gitlab",
    "inrec", "email", "images", "img",
}


def _dominio_base(platform_id: str, pattern: str) -> str | None:
    if platform_id in DOMINI_A_MANO:
        return DOMINI_A_MANO[platform_id]
    # via i gruppi, via gli escape: resta il pezzo letterale
    nudo = re.sub(r"\((?:\?:)?[^)]*\)", "", pattern)
    nudo = nudo.replace("\\.", ".").replace("https://", "")
    nudo = nudo.strip("./")
    # jobs.lever.co/ → jobs.lever.co ; .teamtailor.com → teamtailor.com
    nudo = nudo.split("/")[0].strip(".")
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", nudo):
        return None
    # jobs.lever.co → lever.co (l'archivio si interroga sul registrabile)
    pezzi = nudo.split(".")
    return ".".join(pezzi[-2:])


def _leggi_segnalibri() -> dict:
    try:
        with open(SEGNALIBRI) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _scrivi_segnalibri(seg: dict) -> None:
    os.makedirs(os.path.dirname(SEGNALIBRI), exist_ok=True)
    with open(SEGNALIBRI + ".tmp", "w") as f:
        json.dump(seg, f, indent=1)
    os.replace(SEGNALIBRI + ".tmp", SEGNALIBRI)


def _http(url: str, timeout: int = 90) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _crawl_recenti(quanti: int = 3) -> list[str]:
    dati = json.loads(_http("https://index.commoncrawl.org/collinfo.json",
                            timeout=30))
    return [c["id"] for c in dati[:quanti]]


def _urls_da_cc(dominio: str, crawls: list[str]) -> set[str]:
    """Tutti gli URL archiviati da Common Crawl sotto *.dominio."""
    urls: set[str] = set()
    for cr in crawls:
        q = urllib.parse.quote(f"*.{dominio}", safe="*.")
        base = f"https://index.commoncrawl.org/{cr}-index?url={q}&output=json"
        try:
            for riga in _http(base).splitlines():
                try:
                    urls.add(json.loads(riga)["url"])
                except (ValueError, KeyError):
                    continue
        except Exception as e:                      # noqa: BLE001
            log.info("CC %s su %s: %s", cr, dominio, e)
        time.sleep(2)
    return urls


def _urls_da_wayback(dominio: str, pagina_da: int, pagine_max: int,
                     pausa: float = 10.0) -> tuple[set[str], int, bool]:
    """Una fetta di pagine CDX. Torna (urls, prossima_pagina, finito)."""
    radice = ("https://web.archive.org/cdx/search/cdx"
              f"?url={dominio}&matchType=domain")
    # il conteggio pagine va chiesto NUDO: con fl= o collapse= nel query
    # string, showNumPages risponde "-" invece di un numero
    try:
        np = int(_http(radice + "&showNumPages=true", timeout=60).strip())
    except Exception:                               # noqa: BLE001
        log.info("Wayback non risponde su %s (probabile 429): riprovo "
                 "alla prossima corsa", dominio)
        return set(), pagina_da, False
    base = radice + "&fl=original&collapse=urlkey%3A60"
    urls: set[str] = set()
    p = pagina_da
    fine = min(np, pagina_da + pagine_max)
    while p < fine:
        time.sleep(pausa)
        try:
            testo = _http(f"{base}&page={p}", timeout=120)
        except Exception as e:                      # noqa: BLE001
            log.info("Wayback pagina %d su %s: %s — mi fermo qui",
                     p, dominio, e)
            return urls, p, False
        if testo.lstrip().startswith("<"):          # pagina d'errore 429
            log.info("Wayback 429 su %s a pagina %d — mi fermo qui",
                     dominio, p)
            return urls, p, False
        urls.update(r for r in testo.splitlines() if r)
        p += 1
    return urls, p, p >= np


def _estrai_slug(urls: set[str], pattern: str) -> set[str]:
    rx = re.compile(pattern, re.I)
    slugs: set[str] = set()
    for u in urls:
        m = rx.search(u)
        if not m or not m.groups():
            continue
        s = m.group(1).lower().strip(".-")
        if (s and s not in SLUG_SPAZZATURA and len(s) > 1
                and not re.fullmatch(r"[0-9a-f]{16,}", s)):
            slugs.add(s)
    return slugs


def scopri(dsn: str, piattaforme: list[str] | None, limite_nuove: int,
           pagine_max: int, solo_cc: bool, solo_wayback: bool) -> dict:
    esito = {"piattaforme": 0, "slug_visti": 0, "nuove": 0}
    seg = _leggi_segnalibri()
    crawls = [] if solo_wayback else _crawl_recenti()
    with psycopg.connect(dsn) as conn:
        cur = conn.execute(
            "SELECT id, url_pattern FROM ats_platforms "
            "WHERE url_pattern IS NOT NULL AND is_active ORDER BY id")
        piani = [(pid, pat) for pid, pat in cur.fetchall()
                 if not piattaforme or pid in piattaforme]
        for pid, pat in piani:
            dominio = _dominio_base(pid, pat)
            if not dominio:
                log.info("%s: pattern non derivabile, salto", pid)
                continue
            esito["piattaforme"] += 1
            urls: set[str] = set()
            if not solo_wayback:
                urls |= _urls_da_cc(dominio, crawls)
            if not solo_cc:
                stato = seg.get(pid, {})
                da = 0 if stato.get("finito") else stato.get("pagina", 0)
                wb, prossima, finito = _urls_da_wayback(
                    dominio, da, pagine_max)
                urls |= wb
                seg[pid] = {"pagina": prossima, "finito": finito,
                            "dominio": dominio}
                _scrivi_segnalibri(seg)
            slugs = _estrai_slug(urls, pat)
            esito["slug_visti"] += len(slugs)
            nuove = 0
            for s in sorted(slugs):
                if nuove >= limite_nuove:
                    log.info("%s: tetto di %d nuove raggiunto",
                             pid, limite_nuove)
                    break
                r = conn.execute(
                    "INSERT INTO ats_companies (platform_id, slug, "
                    " company_name, discovered_from) "
                    "VALUES (%s, %s, %s, 'archivio') "
                    "ON CONFLICT (platform_id, slug) DO NOTHING",
                    (pid, s, s.replace("-", " ").title()))
                nuove += r.rowcount
            conn.commit()
            esito["nuove"] += nuove
            log.info("%s (%s): %d slug estratti, %d nuovi",
                     pid, dominio, len(slugs), nuove)
    return esito


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--piattaforme", default="",
                    help="lista separata da virgole; vuoto = tutte")
    ap.add_argument("--limite-nuove", type=int, default=500,
                    help="tetto di tenant nuovi per piattaforma per corsa")
    ap.add_argument("--pagine-max", type=int, default=8,
                    help="pagine Wayback per piattaforma per corsa")
    ap.add_argument("--solo-cc", action="store_true")
    ap.add_argument("--solo-wayback", action="store_true")
    args = ap.parse_args()
    piatt = [p.strip() for p in args.piattaforme.split(",") if p.strip()]
    esito = scopri(ATS_DSN, piatt or None, args.limite_nuove,
                   args.pagine_max, args.solo_cc, args.solo_wayback)
    print(esito)


if __name__ == "__main__":
    main()
