"""HTTP Archive: i siti che USANO un ATS, dal censimento tecnologico.

Ogni mese HTTP Archive scansiona ~16 milioni di siti con traffico reale
e pubblica su BigQuery le tecnologie rilevate (motore Wappalyzer, che
conosce la categoria Recruitment: Greenhouse, Lever, Workable…). Una
query restituisce direttamente «tutti i siti che montano Greenhouse»:
precisione da laboratorio, perche' il rilevamento e' fatto sul codice
della pagina, non indovinato.

Prerequisito (5 minuti, una volta sola):
  1. account Google Cloud + progetto (la fascia gratuita basta:
     1 TB di query al mese, questa ne usa ~50-150 GB);
  2. service account con ruolo «BigQuery Job User», chiave JSON;
  3. la chiave sul server e in .env:
       GOOGLE_APPLICATION_CREDENTIALS=/opt/nivult/gcp-bigquery.json
  4. nel venv:  pip install google-cloud-bigquery

Senza credenziali il modulo si spegne con un messaggio chiaro: niente
mezzi errori. Cadenza naturale: mensile (il dataset si aggiorna cosi').
"""
from __future__ import annotations

import logging
import os

import psycopg

log = logging.getLogger("nivult.ats.tecnologie_ha")

# i nomi con cui Wappalyzer/HTTP Archive chiamano gli ATS che leggiamo
_TECNOLOGIE = (
    "Greenhouse", "Lever", "Workable", "SmartRecruiters", "Recruitee",
    "Teamtailor", "Personio", "BambooHR", "Ashby", "JazzHR", "Breezy HR",
    "iCIMS", "Pinpoint", "Zoho Recruit", "Workday",
)

# BigQuery pretende un filtro LETTERALE sulla partizione `date` (niente
# sottoquery): prima si chiede l'ultima data disponibile, poi la si
# passa come parametro. La prima query tocca solo la colonna date:
# centesimi di GB.
_SQL_DATA = """
SELECT MAX(date) AS d FROM `httparchive.crawl.pages`
 WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
"""

_SQL = """
SELECT DISTINCT page
  FROM `httparchive.crawl.pages`,
       UNNEST(technologies) AS t
 WHERE date = @data
   AND client = 'mobile'
   AND is_root_page
   AND t.technology IN UNNEST(@tecnologie)
"""


def raccogli(dsn: str) -> dict:
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        raise SystemExit(
            "Serve GOOGLE_APPLICATION_CREDENTIALS (vedi docstring: "
            "account GCP gratuito + service account BigQuery Job User).")
    try:
        from google.cloud import bigquery
    except ImportError:
        raise SystemExit("Serve: pip install google-cloud-bigquery")

    from nivult.ats.riscoperta import _radice
    cli = bigquery.Client()
    data = list(cli.query(_SQL_DATA).result())[0].d
    log.info("http_archive: uso la scansione del %s", data)
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("data", "DATE", data),
        bigquery.ArrayQueryParameter("tecnologie", "STRING",
                                     list(_TECNOLOGIE))])
    stats = {"pagine": 0, "domini_nuovi": 0}
    lotto: list[tuple] = []
    visti: set[str] = set()
    with psycopg.connect(dsn, autocommit=True) as conn:
        def scrivi():
            nonlocal lotto
            if lotto:
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO company_domains (domain, source)
                           VALUES (%s, 'http_archive')
                           ON CONFLICT (domain) DO NOTHING""", lotto)
                lotto = []
        for riga in cli.query(_SQL, job_config=cfg).result():
            stats["pagine"] += 1
            host = (riga.page or "").split("//")[-1].split("/")[0].lower()
            dominio = _radice(host)
            if dominio and dominio not in visti:
                visti.add(dominio)
                lotto.append((dominio,))
                if len(lotto) >= 1000:
                    scrivi()
        scrivi()
    stats["domini_nuovi"] = len(visti)
    log.info("http_archive: %s", stats)
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
