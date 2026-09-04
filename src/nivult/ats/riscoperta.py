"""Riscoperta: le offerte che abbiamo gia' ci indicano aziende nuove.

La fonte di domini col rendimento migliore non e' un registro esterno ma il
nostro stesso raccolto: i domini estratti dalle offerte vere arrivano a un
ATS il 43% delle volte, contro lo 0,1% dei domini indovinati da GLEIF —
perche' sono aziende che stanno DAVVERO assumendo. Qui setacciamo il `raw`
e gli URL delle offerte per due segnali:

1. `hiringOrganization.url` (JSON-LD): il sito ufficiale del datore.
2. l'host dell'offerta quando NON e' un ATS noto (careers.dhl.com,
   jobs.kbr.com): il dominio radice e' il datore.

I domini nuovi entrano in `company_domains` (source='reverse') e il
detector notturno li lavora: careers -> ATS -> tenant.
"""
from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

import psycopg

log = logging.getLogger("nivult.ats.riscoperta")

# host che appartengono agli ATS/board, non ai datori: da scartare
_ATS_HOST = re.compile(
    r"greenhouse|lever\.co|workable|ashbyhq|smartrecruiters|recruitee|"
    r"myworkdayjobs|bamboohr|breezy\.hr|teamtailor|personio|icims|"
    r"zohorecruit|catsone|jobscore|hirehive|applytojob|applicantstack|"
    r"trakstar|niceboard|traffit|vincere\.io|taleez|heavenhr|jobsoid|"
    r"pinpointhq|join\.com|eploy|softgarden|werecruit|arbeitsagentur|"
    r"digitalrecruiters|crelate|hiringthing|successfactors|oraclecloud|"
    r"eightfold|avature|phenom|jobvite|cornerstone|csod\.com|taleo|"
    r"adp\.com|paylocity|dvinci|talentsoft|intervieweb|arbetsformedlingen|"
    r"europa\.eu|francetravail|hirehive|homerun|freshteam|comeet|"
    r"pageup|radancy|jibeapply|smartjobboard|jazz\.co|jazzhr", re.I)

# suffissi a due livelli piu' comuni (co.uk, com.au...): per non troncare male
_TLD2 = {"co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au",
         "co.nz", "co.jp", "co.in", "com.br", "com.mx", "com.tr", "com.sg",
         "com.hk", "co.za", "com.pl", "com.cn"}

_PREFISSI = ("www.", "careers.", "career.", "jobs.", "job.", "work.",
             "lavora.", "lavoraconnoi.", "recrutement.", "karriere.",
             "empleo.", "vacatures.", "jobb.", "stellen.", "recruiting.",
             "talent.", "apply.", "join.", "hiring.", "portal.")


def _radice(host: str) -> str | None:
    """careers.dhl.com -> dhl.com; jobs.bbc.co.uk -> bbc.co.uk."""
    h = (host or "").lower().strip(".")
    if not h or "." not in h or _ATS_HOST.search(h):
        return None
    for p in _PREFISSI:
        if h.startswith(p):
            h = h[len(p):]
            break
    parti = h.split(".")
    if len(parti) < 2:
        return None
    if ".".join(parti[-2:]) in _TLD2 and len(parti) >= 3:
        dominio = ".".join(parti[-3:])
    else:
        dominio = ".".join(parti[-2:])
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]+\.[a-z]{2,}", dominio):
        return None
    return dominio


def riscopri(dsn: str) -> dict:
    stats = {"candidati": 0, "nuovi": 0}
    domini: dict[str, str | None] = {}   # dominio -> nome azienda (se noto)
    with psycopg.connect(dsn, autocommit=True) as c:
        # 1) hiringOrganization.url dal JSON-LD
        for url, nome in c.execute("""
            SELECT raw->'hiringOrganization'->>'url',
                   raw->'hiringOrganization'->>'name'
              FROM ats_jobs
             WHERE raw->'hiringOrganization' ? 'url'
               AND expired_at IS NULL""").fetchall():
            d = _radice(urlparse(url or "").hostname or "")
            if d:
                domini.setdefault(d, nome)
        # 2) host dell'offerta quando non e' un ATS noto
        for (host,) in c.execute("""
            SELECT DISTINCT split_part(split_part(url,'//',2),'/',1)
              FROM ats_jobs WHERE expired_at IS NULL
               AND url LIKE 'http%'""").fetchall():
            d = _radice(host)
            if d:
                domini.setdefault(d, None)
        stats["candidati"] = len(domini)
        for dominio, nome in domini.items():
            r = c.execute("""
                INSERT INTO company_domains (domain, company_name, source)
                VALUES (%s, %s, 'reverse')
                ON CONFLICT (domain) DO NOTHING""", (dominio, nome)).rowcount
            stats["nuovi"] += r
    log.info("riscoperta: %s", stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(riscopri(dsn))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
