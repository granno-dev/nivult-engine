"""Censimento completo dei tenant ATS dall'indice colonnare di Common Crawl.

La scoperta quotidiana (scoperta_archivi) pesca a pagine dal CDX della
Wayback e dall'indice classico di Common Crawl: trova, ma a campione.
Qui si fa il censimento INTERO: l'indice colonnare (cc-index-table,
~300 parquet per crawl) elenca ogni URL visto nel crawl, e con DuckDB
in lettura remota si filtrano solo i row group che contengono i domini
degli ATS. Una passata = tutti i tenant di tutte le piattaforme che
leggiamo, non un campione.

Per ogni piattaforma con url_pattern in ats_platforms si ricava il
dominio registrabile (stessa logica di scoperta_archivi), si raccolgono
gli (host, path) distinti e si applica il pattern: ogni cattura e' uno
slug di tenant ESISTITO davvero nel crawl. I nuovi entrano in
ats_companies con discovered_from='cc_indice'; poi il raccoglitore li
visita e tiene i vivi.

Uso:
  python -m nivult.ats.censimento_cc [--crawl CC-MAIN-2026-38]
      [--da N] [--prova] [--limite-nuove N]

--da N riprende dal parquet N dopo un'interruzione; --prova conta e
mostra senza scrivere nel database.
"""

from __future__ import annotations

import argparse
import gzip
import logging
import time
import urllib.request

import duckdb
import psycopg

from .runner import ATS_DSN
from .scoperta_archivi import _crawl_recenti, _dominio_base, _estrai_slug

log = logging.getLogger("nivult.ats.censimento_cc")

BASE = "https://data.commoncrawl.org/"
UA = "nivult-engine/1.0 (tenant census; hello@nivult.com)"


def _parquet_del_crawl(crawl: str) -> list[str]:
    """I ~300 path dei parquet subset=warc del crawl."""
    req = urllib.request.Request(
        BASE + f"crawl-data/{crawl}/cc-index-table.paths.gz",
        headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        righe = gzip.decompress(r.read()).decode().splitlines()
    return [x.strip() for x in righe if "subset=warc" in x]


def _piattaforme(dsn: str) -> dict[str, list[tuple[str, str]]]:
    """dominio registrabile -> [(platform_id, url_pattern), ...]."""
    per_dominio: dict[str, list[tuple[str, str]]] = {}
    with psycopg.connect(dsn) as conn:
        righe = conn.execute(
            "SELECT id, url_pattern FROM ats_platforms "
            "WHERE url_pattern IS NOT NULL AND is_active ORDER BY id"
        ).fetchall()
    for pid, pat in righe:
        dom = _dominio_base(pid, pat)
        if not dom:
            log.info("%s: dominio non derivabile dal pattern, salto", pid)
            continue
        per_dominio.setdefault(dom, []).append((pid, pat))
    return per_dominio


def censimento(dsn: str, crawl: str | None, da: int, prova: bool,
               limite_nuove: int) -> dict:
    t0 = time.time()
    crawl = crawl or _crawl_recenti(1)[0]
    log.info("crawl: %s", crawl)
    files = _parquet_del_crawl(crawl)
    log.info("%d parquet da scandire", len(files))

    per_dominio = _piattaforme(dsn)
    domini = sorted(per_dominio)
    log.info("%d piattaforme su %d domini: %s",
             sum(len(v) for v in per_dominio.values()), len(domini),
             ", ".join(domini))

    # Il server ha 7,6 GB e ci vivono Postgres e i demoni: il primo giro
    # senza argini e' finito con l'OOM killer che ha abbattuto il giro E
    # il servizio di arricchimento (02/10/2026). Memoria ribassata,
    # spill sul volume dati, mai piu' senza.
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("SET threads=2")
    con.execute("SET memory_limit='700MB'")
    con.execute("SET temp_directory='/mnt/HC_Volume_106941692/tmp-duckdb'")
    elenco = "','".join(domini)

    # slug per piattaforma, accumulati su tutto il crawl
    slugs: dict[str, set[str]] = {pid: set()
                                  for v in per_dominio.values()
                                  for pid, _ in v}
    for i, f in enumerate(files):
        if i < da:
            continue
        t = time.time()
        righe = None
        for tentativo in range(4):
            # data.commoncrawl.org strozza dopo ~260 richieste fitte
            # (visto il 02/10/2026: i parquet 267-299 tutti in errore):
            # passo calmo e riprova prima di dichiarare il file perso
            try:
                righe = con.execute(f"""
                    SELECT DISTINCT url_host_name, url_path
                      FROM read_parquet('{BASE}{f}')
                     WHERE url_host_registered_domain IN ('{elenco}')
                       AND fetch_status = 200
                       AND length(url_path) < 100""").fetchall()
                break
            except Exception as e:                  # noqa: BLE001
                log.warning("parquet %d: %.80s — riprovo tra poco", i, e)
                time.sleep(20 * (tentativo + 1))
        if righe is None:
            log.warning("parquet %d: PERSO dopo 4 tentativi", i)
            continue
        for host, path in righe:
            url = f"https://{host}{path}"
            for dom, piani in per_dominio.items():
                if not host.endswith(dom):
                    continue
                for pid, pat in piani:
                    slugs[pid] |= _estrai_slug({url}, pat)
        log.info("parquet %d/%d: %d righe in %ds (parziale slug: %d)",
                 i + 1, len(files), len(righe), int(time.time() - t),
                 sum(len(s) for s in slugs.values()))

    stats = {"crawl": crawl, "slug_per_piattaforma": {}, "nuove": 0}
    with psycopg.connect(dsn, autocommit=True) as conn:
        for pid, trovati in sorted(slugs.items()):
            nuove = 0
            if not prova:
                for s in sorted(trovati):
                    if nuove >= limite_nuove:
                        log.warning("%s: tetto di %d nuove raggiunto",
                                    pid, limite_nuove)
                        break
                    r = conn.execute(
                        "INSERT INTO ats_companies (platform_id, slug,"
                        " company_name, discovered_from) "
                        "VALUES (%s, %s, %s, 'cc_indice') "
                        "ON CONFLICT (platform_id, slug) DO NOTHING",
                        (pid, s, s.replace("-", " ").title()))
                    nuove += r.rowcount
            stats["slug_per_piattaforma"][pid] = {
                "trovati": len(trovati), "nuovi": nuove}
            stats["nuove"] += nuove
            log.info("%s: %d slug nel crawl, %d nuovi%s",
                     pid, len(trovati), nuove, " (prova)" if prova else "")
    log.info("censimento chiuso in %.0f min: %s",
             (time.time() - t0) / 60, stats)
    return stats


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.censimento_cc")
    ap.add_argument("--crawl", default="",
                    help="id del crawl (vuoto = l'ultimo)")
    ap.add_argument("--da", type=int, default=0,
                    help="riparti dal parquet N dopo un'interruzione")
    ap.add_argument("--prova", action="store_true",
                    help="conta e mostra, non scrivere nel database")
    ap.add_argument("--limite-nuove", type=int, default=100000,
                    help="tetto di tenant nuovi per piattaforma")
    a = ap.parse_args()
    censimento(ATS_DSN, a.crawl or None, a.da, a.prova, a.limite_nuove)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
