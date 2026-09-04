"""Scoperta profonda dei grandi ATS sotto-censiti, verificata e resumibile.

La scoperta ordinaria cappa a ~500 nuovi per giro e visita ogni ATS a
rotazione (una notte su venti): per i colossi come Greenhouse — che negli
archivi hanno decine di migliaia di board — cresce troppo lenta, e ne
restano censiti pochi. Questo modulo tira MOLTI token da Wayback per una
singola piattaforma, li **verifica** contro l'adapter reale (solo i board
vivi con offerte entrano: la qualita' e' garantita), e riprende da dove ha
lasciato grazie a un segnalibro di pagina — cosi' un colosso si drena in
piu' sessioni senza maltrattare l'archivio ne' sporcare il censimento.
"""
from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg

from nivult.ats.adapters import ADAPTERS

log = logging.getLogger("nivult.ats.profonda")

_UA = "nivult-ats/1.0"
_STATO = "/opt/nivult/ats-nightly-state"

# host-pattern e regex del token, per le piattaforme a board pubblico.
_FONTI: dict[str, tuple[str, str]] = {
    "greenhouse":  ("boards.greenhouse.io/*",   r"boards\.greenhouse\.io/([A-Za-z0-9]+)"),
    "lever":       ("jobs.lever.co/*",          r"jobs\.lever\.co/([A-Za-z0-9][A-Za-z0-9._-]+)"),
    "workable":    ("apply.workable.com/*",     r"apply\.workable\.com/([a-z0-9][a-z0-9-]+)"),
    "ashby":       ("jobs.ashbyhq.com/*",       r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9._-]+)"),
    "smartrecruiters": ("careers.smartrecruiters.com/*", r"careers\.smartrecruiters\.com/([A-Za-z0-9][A-Za-z0-9-]+)"),
    "recruitee":   ("*.recruitee.com",          r"([a-z0-9][a-z0-9-]+)\.recruitee\.com"),
    "personio":    ("*.jobs.personio.com",      r"([a-z0-9][a-z0-9-]+)\.jobs\.personio\."),
    "bamboohr":    ("*.bamboohr.com/careers",   r"([a-z0-9][a-z0-9-]+)\.bamboohr\.com"),
    "breezy":      ("*.breezy.hr",              r"([a-z0-9][a-z0-9-]+)\.breezy\.hr"),
}

_STOP = {"www", "api", "app", "jobs", "careers", "static", "assets", "cdn",
         "embed", "boards", "job-boards", "12345", "test", "demo", "example"}


def _segnalibro(pid: str) -> int:
    try:
        with open(os.path.join(_STATO, f"profonda-{pid}.txt")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def _salva_segnalibro(pid: str, pagina: int) -> None:
    try:
        os.makedirs(_STATO, exist_ok=True)
        with open(os.path.join(_STATO, f"profonda-{pid}.txt"), "w") as f:
            f.write(str(pagina))
    except OSError:
        pass


def _wayback_pagina(cli: httpx.Client, pattern: str, pagina: int) -> str:
    url = ("http://web.archive.org/cdx/search/cdx?url=" + pattern +
           "&output=text&fl=original&collapse=urlkey&page=" + str(pagina))
    for tentativo in range(3):
        try:
            r = cli.get(url, timeout=45)
            if r.status_code == 200:
                return r.text
        except httpx.HTTPError:
            time.sleep(2 * (tentativo + 1))
    return ""


def _verifica(pid: str, slug: str) -> int:
    cls = ADAPTERS.get(pid)
    if cls is None:
        return 0
    ad = cls()
    try:
        return len(ad.jobs(slug))
    except Exception:                                # noqa: BLE001
        return 0
    finally:
        try:
            ad.close()
        except Exception:                            # noqa: BLE001
            pass


def scava(dsn: str, platform_id: str, pagine: int = 8,
          verifica_max: int = 800) -> dict:
    """Una sessione di scoperta profonda per una piattaforma."""
    if platform_id not in _FONTI:
        return {"errore": f"piattaforma {platform_id} non gestita"}
    pattern, rx = _FONTI[platform_id]
    rex = re.compile(rx, re.I)
    cli = httpx.Client(headers={"User-Agent": _UA}, follow_redirects=True)
    conn = psycopg.connect(dsn, autocommit=True)

    noti = set(s.lower() for (s,) in conn.execute(
        "SELECT slug FROM ats_companies WHERE platform_id=%s",
        (platform_id,)).fetchall())

    da = _segnalibro(platform_id)
    token: set[str] = set()
    pagina = da
    for _ in range(pagine):
        testo = _wayback_pagina(cli, pattern, pagina)
        pagina += 1
        if not testo:
            continue
        for riga in testo.splitlines():
            m = rex.search(riga)
            if m:
                t = m.group(1).lower()
                if t not in _STOP and len(t) >= 2:
                    token.add(t)
    _salva_segnalibro(platform_id, pagina)

    nuovi = [t for t in token if t not in noti][:verifica_max]
    stats = {"piattaforma": platform_id, "pagine": pagine,
             "da_pagina": da, "token_visti": len(token),
             "nuovi_candidati": len(nuovi), "vivi": 0, "offerte": 0}

    def _prova(slug):
        n = _verifica(platform_id, slug)
        return (slug, n)

    with ThreadPoolExecutor(max_workers=24) as ex:
        for slug, n in ex.map(_prova, nuovi):
            if n > 0:
                conn.execute(
                    "INSERT INTO ats_companies (platform_id, slug, discovered_from)"
                    " VALUES (%s, %s, 'profonda') "
                    "ON CONFLICT (platform_id, slug) DO NOTHING",
                    (platform_id, slug))
                stats["vivi"] += 1
                stats["offerte"] += n
    cli.close()
    log.info("profonda %s: %s", platform_id, stats)
    return stats


def demone(dsn: str, pagine: int = 4, pausa: int = 25) -> None:
    """Gira senza sosta, ruotando su TUTTE le piattaforme a token.

    Ogni giro fa qualche pagina di Wayback per una piattaforma e passa
    alla successiva, con una pausa breve per non maltrattare l'archivio.
    Quando una piattaforma esaurisce le pagine (nessun candidato nuovo),
    il suo segnalibro riparte da zero: cosi' ri-scandisce dall'inizio e
    intercetta le aziende appena comparse. Il censimento di tutti gli ATS
    cresce di continuo, non solo di notte.
    """
    piattaforme = list(_FONTI.keys())
    i = 0
    log.info("scoperta profonda: demone avviato su %d piattaforme", len(piattaforme))
    while True:
        pid = piattaforme[i % len(piattaforme)]
        i += 1
        try:
            r = scava(dsn, pid, pagine=pagine, verifica_max=400)
            # se non ha trovato candidati nuovi, la piattaforma e' drenata:
            # riparti dall'inizio per intercettare i tenant nuovi.
            if r.get("nuovi_candidati", 0) == 0:
                _salva_segnalibro(pid, 0)
            log.info("profonda %s: +%d vivi (%d offerte), pag da %d",
                     pid, r.get("vivi", 0), r.get("offerte", 0),
                     r.get("da_pagina", 0))
        except Exception as exc:                     # noqa: BLE001
            log.warning("profonda %s errore: %s", pid, exc)
        time.sleep(pausa)


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.scoperta_profonda")
    ap.add_argument("--piattaforma")
    ap.add_argument("--demone", action="store_true",
                    help="gira in continuo su tutte le piattaforme a token")
    ap.add_argument("--pagine", type=int, default=8)
    ap.add_argument("--verifica-max", type=int, default=800)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    if args.demone:
        demone(dsn, pagine=max(3, args.pagine // 2))
        return 0
    if not args.piattaforma:
        ap.error("serve --piattaforma oppure --demone")
    print(scava(dsn, args.piattaforma, args.pagine, args.verifica_max))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
