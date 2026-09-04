"""Potatura degli slug morti: disattiva i tenant che non esistono piu'.

La scoperta da Common Crawl/Wayback accumula slug nel tempo, e molti muoiono
(l'azienda chiude l'account ATS): restano in censimento con job_count=0,
indistinguibili dai vivi-ma-vuoti, e lo scraping li rivisita a vuoto ogni
ciclo. Qui, per i tenant senza offerte, interroghiamo l'endpoint di liveness
della piattaforma: se risponde 404/errore in modo netto lo marchiamo
`is_active=false` (lo smettiamo di scrapare); se risponde 200 (vivo ma vuoto)
lo teniamo. Cosi' il censimento resta pulito, l'attivazione diventa un numero
vero, e la capacita' liberata va ai tenant vivi.

Non tocca chi ha offerte, ne' i mai-visti: solo i visitati-vuoti stantii.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor

import httpx
import psycopg

log = logging.getLogger("nivult.ats.potatura")

_UA = "Mozilla/5.0 (compatible; nivult-ats/1.0)"

# endpoint di liveness per piattaforma: (metodo, template). Un 404/410 o un
# errore di connessione netto = slug morto. Un 200 = vivo (anche se vuoto).
_VIVO = {
    "lever":           ("GET",  "https://api.lever.co/v0/postings/{s}?mode=json"),
    "greenhouse":      ("GET",  "https://boards-api.greenhouse.io/v1/boards/{s}/jobs?content=false"),
    "ashby":           ("POST", "https://api.ashbyhq.com/posting-api/job-board/{s}"),
    "workable":        ("GET",  "https://apply.workable.com/api/v1/widget/accounts/{s}?details=false"),
    "recruitee":       ("GET",  "https://{s}.recruitee.com/api/offers/"),
    "smartrecruiters": ("GET",  "https://api.smartrecruiters.com/v1/companies/{s}/postings?limit=1"),
    "breezy":          ("GET",  "https://{s}.breezy.hr/json"),
    "personio":        ("GET",  "https://{s}.jobs.personio.com/xml"),
    "jobsoid":         ("GET",  "https://{s}.jobsoid.com/api/v1/jobs"),
    "recruiterbox":    ("GET",  "https://{s}.hire.trakstar.com/jobfeeds/{s}"),
    "niceboard":       ("GET",  "https://{s}.niceboard.co/api/jobs?limit=1"),
    "jazzhr":          ("GET",  "https://{s}.applytojob.com/"),
    "teamtailor":      ("GET",  "https://{s}.teamtailor.com/jobs.json"),
}

# Piattaforme dove lo slug morto NON risponde 404 ma REINDIRIZZA al sito
# vetrina (jazzhr -> info.jazzhr.com): li' il redirect stesso e' la morte,
# quindi non lo si segue e un 3xx conta come slug morto.
_MORTO_SU_REDIRECT = {"jazzhr"}


def pota(dsn: str, platform_id: str, limite: int = 1000) -> dict:
    if platform_id not in _VIVO:
        return {"errore": f"{platform_id} senza endpoint di liveness"}
    metodo, tmpl = _VIVO[platform_id]
    cli = httpx.Client(timeout=12, follow_redirects=True,
                       headers={"User-Agent": _UA})
    stats = {"piattaforma": platform_id, "esaminati": 0,
             "morti": 0, "vivi": 0}
    with psycopg.connect(dsn, autocommit=True) as c:
        righe = c.execute("""
            SELECT slug FROM ats_companies
             WHERE platform_id = %s AND is_active
               AND job_count = 0
               AND last_fetch_at < now() - interval '2 hours'
             ORDER BY last_fetch_at ASC NULLS FIRST
             LIMIT %s""", (platform_id, limite)).fetchall()

        segui = platform_id not in _MORTO_SU_REDIRECT

        def _stato(slug):
            url = tmpl.format(s=slug)
            try:
                r = (cli.post(url, json={}) if metodo == "POST"
                     else cli.get(url, follow_redirects=segui))
                return slug, r.status_code
            except httpx.HTTPError:
                return slug, -1       # errore di rete transitorio: non potare

        with ThreadPoolExecutor(max_workers=20) as ex:
            for slug, code in ex.map(_stato, [r[0] for r in righe]):
                stats["esaminati"] += 1
                # morto SOLO se 404/410 (definitivo). 429/500/403/rete sono
                # transitori: NON si pota, per non disattivare un vivo.
                if code in (404, 410) or (not segui and 300 <= code < 400):
                    c.execute("UPDATE ats_companies SET is_active=false "
                              "WHERE platform_id=%s AND slug=%s",
                              (platform_id, slug))
                    stats["morti"] += 1
                else:
                    stats["vivi"] += 1
    cli.close()
    return stats


def main(argv: list[str] | None = None) -> int:
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="nivult.ats.potatura")
    ap.add_argument("--piattaforma", required=True)
    ap.add_argument("--limite", type=int, default=1000)
    args = ap.parse_args(argv)
    dsn = os.environ.get(
        "ATS_DATABASE_URL",
        "postgresql://giusepperanno@127.0.0.1:5432/nivult_ats")
    print(pota(dsn, args.piattaforma, args.limite))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
