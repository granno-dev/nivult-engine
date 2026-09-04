"""Il grafo dei link di Common Crawl: chi punta a una board ATS assume.

Common Crawl pubblica ogni trimestre il grafo host->host del web (~235M
host, ~3 miliardi di archi). La domanda mirata: quali host LINKANO le
board degli ATS (jobs.lever.co, boards.greenhouse.io, *.recruitee.com…)?
Quasi sempre e' il sito del datore stesso — il gap-vanity che ci separa
dai grandi player, pescato all'ingrosso.

Tre passate in streaming puro (niente disco):
  A. vertici: gli ID degli host-bersaglio ATS. I nomi sono ROVESCIATI
     (jobs.lever.co -> co.lever.jobs), quindi il match di suffisso
     tenant diventa un prefisso: veloce e senza regex.
  B. archi (src, dst): i src che puntano a un bersaglio.
  C. vertici di nuovo: da ID a host, ridotti al dominio radice con le
     regole della riscoperta (che scarta gli host ATS), e inseriti in
     company_domains source='grafo_cc' — poi detector -> vanity.

Batch mensile: ore di streaming, zero pretese di real-time. Il flusso
CT (certificati.py) copre il fresco; questo copre il pregresso.
"""
from __future__ import annotations

import gzip
import logging
import zlib
import os
import time

import httpx
import psycopg

from nivult.ats.riscoperta import _radice

log = logging.getLogger("nivult.ats.grafo_cc")

BASE = "https://data.commoncrawl.org/"
RILASCIO = "projects/hyperlinkgraph/cc-main-2026-jun-jul-aug/host/"

# bersagli in notazione ROVESCIATA: esatti e prefissi-tenant
_ESATTI = {
    b"co.lever.jobs", b"io.greenhouse.boards", b"io.greenhouse.job-boards",
    b"io.greenhouse.eu.job-boards", b"com.workable.apply",
    b"com.ashbyhq.jobs", b"com.smartrecruiters.careers",
    b"com.smartrecruiters.jobs", b"com.rippling.ats",
}
_PREFISSI = (
    b"com.recruitee.", b"com.teamtailor.", b"hr.breezy.",
    b"com.bamboohr.", b"com.myworkdayjobs.", b"com.icims.",
    b"co.applytojob.", b"com.pinpointhq.", b"io.vincere.",
    b"com.applicantstack.", b"com.freshteam.", b"com.zohorecruit.",
    b"eu.zohorecruit.", b"com.jobsoid.", b"co.niceboard.",
    b"com.csod.", b"io.softgarden.", b"it.intervieweb.",
    b"com.jobs.personio.", b"de.jobs.personio.",
)


def _bersaglio(rev: bytes) -> bool:
    return rev in _ESATTI or rev.startswith(_PREFISSI)


def _parti(cli: httpx.Client, nome: str) -> list[str]:
    r = cli.get(BASE + RILASCIO + nome)
    r.raise_for_status()
    return gzip.decompress(r.content).decode().split()


def _stream(cli: httpx.Client, percorso: str):
    """Le righe di un part .gz, in streaming senza toccare disco."""
    with cli.stream("GET", BASE + percorso) as r:
        r.raise_for_status()
        resto = b""
        decomp = zlib.decompressobj(16 + 15)
        for pezzo in r.iter_bytes(1 << 20):
            dati = resto + decomp.decompress(pezzo)
            righe = dati.split(b"\n")
            resto = righe.pop()
            yield from righe
        if resto:
            yield resto


def raccogli(dsn: str) -> dict:
    stats = {"bersagli": 0, "sorgenti": 0, "host": 0, "domini_nuovi": 0}
    cli = httpx.Client(timeout=120, headers={"User-Agent": "nivult-ats/1.0"})
    t0 = time.time()

    vert = _parti(cli, "cc-main-2026-jun-jul-aug-host-vertices.paths.gz")
    archi = _parti(cli, "cc-main-2026-jun-jul-aug-host-edges.paths.gz")

    # ── A: gli ID dei bersagli ──
    bersagli: set[bytes] = set()
    for i, parte in enumerate(vert):
        for riga in _stream(cli, parte):
            vid, _, rev = riga.partition(b"\t")
            if _bersaglio(rev):
                bersagli.add(vid)
        log.info("A vertici %d/%d · bersagli %d · %.0fs",
                 i + 1, len(vert), len(bersagli), time.time() - t0)
    stats["bersagli"] = len(bersagli)

    # ── B: chi li punta ──
    sorgenti: set[bytes] = set()
    for i, parte in enumerate(archi):
        for riga in _stream(cli, parte):
            src, _, dst = riga.partition(b"\t")
            if dst in bersagli:
                sorgenti.add(src)
        if (i + 1) % 20 == 0 or i + 1 == len(archi):
            log.info("B archi %d/%d · sorgenti %d · %.0fs",
                     i + 1, len(archi), len(sorgenti), time.time() - t0)
        if len(sorgenti) > 20_000_000:
            log.warning("troppe sorgenti: mi fermo qui per prudenza")
            break
    stats["sorgenti"] = len(sorgenti)

    # ── C: da ID a dominio, dentro il volano ──
    lotto: list[tuple] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        def scrivi():
            nonlocal lotto
            if lotto:
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO company_domains (domain, source)
                           VALUES (%s, 'grafo_cc')
                           ON CONFLICT (domain) DO NOTHING""", lotto)
                lotto = []
        visti: set[str] = set()
        for i, parte in enumerate(vert):
            for riga in _stream(cli, parte):
                vid, _, rev = riga.partition(b"\t")
                if vid not in sorgenti:
                    continue
                stats["host"] += 1
                host = ".".join(reversed(
                    rev.decode("ascii", "ignore").split(".")))
                dominio = _radice(host)
                if dominio and dominio not in visti:
                    visti.add(dominio)
                    lotto.append((dominio,))
                    if len(lotto) >= 1000:
                        scrivi()
            log.info("C vertici %d/%d · domini %d · %.0fs",
                     i + 1, len(vert), len(visti), time.time() - t0)
        scrivi()
        stats["domini_nuovi"] = len(visti)
    cli.close()
    log.info("grafo_cc: %s in %.0f min", stats, (time.time() - t0) / 60)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(raccogli(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
