"""La scheda azienda che il mercato vende: i campi di Coresignal che si ricavano
da cio' che abbiamo gia', senza fonti chiuse (21/09/2026).

Coresignal incolla su ogni annuncio 24 campi di azienda presi da LinkedIn,
Glassdoor e Indeed. Noi quelle fonti non le tocchiamo; ma buona parte di
quei campi si DEDUCE dalle offerte stesse e dalle fonti aperte che gia'
leggiamo, e si etichetta per quello che e':

  size_range        fascia di dipendenti (stile LinkedIn) dal numero migliore
                    che abbiamo: registro > sito > dichiarato > wikidata
  locations         le sedi VISTE nelle offerte, con quante offerte ciascuna;
                    la prima e' la piu' frequente (is_primary)
  hq_*              la sede principale: dal registro/GLEIF se ce l'abbiamo,
                    altrimenti la sede piu' frequente delle offerte, e
                    `hq_da` dice quale delle due e'
  description       la descrizione che l'azienda da' di se' negli annunci
                    (jsonld hiringOrganization.description, SmartRecruiters
                    companyDescription): parole SUE, non nostre
  keywords          le famiglie e le tecnologie piu' frequenti nei suoi annunci
  external_urls     sameAs / url dichiarati (sito, LinkedIn, ecc.)

Una riga per tenant in `aziende_dettagli`; tutto SQL e un giro Python al
giorno, niente modelli, niente GPU.

    python -m nivult.ats.aziende_dettagli [--limite N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time

import psycopg

log = logging.getLogger("nivult.ats.aziende_dettagli")

DDL = """
CREATE TABLE IF NOT EXISTS aziende_dettagli (
  company_id     uuid PRIMARY KEY REFERENCES ats_companies(id) ON DELETE CASCADE,
  size_range     text,
  size_da        text,
  employees_best integer,
  locations      jsonb,       -- [{city, country, state, offerte, is_primary}]
  n_locations    integer,
  hq_country     text,
  hq_state       text,
  hq_city        text,
  hq_street      text,
  hq_zipcode     text,
  hq_full_address text,
  hq_da          text,        -- registro / gleif / offerte
  description    text,
  description_da text,
  external_urls  jsonb,
  keywords       jsonb,       -- {famiglie: [...], tecnologie: [...]}
  calcolato_at   timestamptz NOT NULL DEFAULT now());
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS dettagli_at timestamptz;
"""

FASCE = ((1, 10, "1-10"), (11, 50, "11-50"), (51, 200, "51-200"), (201, 500, "201-500"), (501, 1000, "501-1000"),
         (1001, 5000, "1001-5000"), (5001, 10000, "5001-10000"), (10001, 10**9, "10001+"))


def fascia(n: int | None) -> str | None:
    if not n or n <= 0:
        return None
    for a, b, et in FASCE:
        if a <= n <= b:
            return et
    return None


SQL_TENANT = """
SELECT c.id, c.employees_reg, c.employees_site, c.employees_self_n, c.employees_wd, c.employees_reg_band, c.country
  FROM ats_companies c
 WHERE c.job_count > 0 AND (c.dettagli_at IS NULL OR c.dettagli_at < now() - interval '7 days')
 ORDER BY c.job_count DESC
 LIMIT %s
"""
SQL_SEDI = """
SELECT coalesce(j.city, ''), coalesce(j.country, ''), coalesce(d.state, ''), count(*) AS n
  FROM ats_jobs j LEFT JOIN offerte_dettagli d ON d.job_id = j.id
 WHERE j.platform_id = %s AND j.slug = %s AND j.expired_at IS NULL
 GROUP BY 1, 2, 3 ORDER BY n DESC LIMIT 25
"""
SQL_FAMIGLIE = """
SELECT coalesce(x.family, x.v1_family) AS f, count(*) AS n
  FROM ats_jobs j JOIN job_classifications x ON x.job_id = j.id
 WHERE j.platform_id = %s AND j.slug = %s AND j.expired_at IS NULL AND coalesce(x.family, x.v1_family) IS NOT NULL
 GROUP BY 1 ORDER BY n DESC LIMIT 5
"""
SQL_TEC = """
SELECT technology, annunci FROM azienda_tecnologie WHERE platform_id = %s AND slug = %s
 ORDER BY annunci_attivi DESC, annunci DESC LIMIT 12
"""
SQL_ORG = """
SELECT j.raw->'hiringOrganization', j.raw->'jobAd'->'sections'->'companyDescription'->>'text'
  FROM ats_jobs j
 WHERE j.platform_id = %s AND j.slug = %s AND j.expired_at IS NULL
   AND (j.raw ? 'hiringOrganization' OR j.raw->'jobAd'->'sections' ? 'companyDescription')
 ORDER BY j.posted_at DESC NULLS LAST LIMIT 3
"""
SQL_SCRIVI = """
INSERT INTO aziende_dettagli (company_id, size_range, size_da, employees_best, locations, n_locations,
                              hq_country, hq_state, hq_city, hq_street, hq_zipcode, hq_full_address, hq_da,
                              description, description_da, external_urls, keywords)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (company_id) DO UPDATE SET
  size_range = EXCLUDED.size_range, size_da = EXCLUDED.size_da, employees_best = EXCLUDED.employees_best,
  locations = EXCLUDED.locations, n_locations = EXCLUDED.n_locations,
  hq_country = EXCLUDED.hq_country, hq_state = EXCLUDED.hq_state, hq_city = EXCLUDED.hq_city,
  hq_street = EXCLUDED.hq_street, hq_zipcode = EXCLUDED.hq_zipcode, hq_full_address = EXCLUDED.hq_full_address,
  hq_da = EXCLUDED.hq_da, description = EXCLUDED.description, description_da = EXCLUDED.description_da,
  external_urls = EXCLUDED.external_urls, keywords = EXCLUDED.keywords, calcolato_at = now()
"""
_TAG = re.compile(r"<[^>]+>")


def _pulito(t) -> str:
    import html as _html
    t = t if isinstance(t, str) else ""
    return re.sub(r"\s+", " ", _TAG.sub(" ", _html.unescape(_html.unescape(t)))).strip()


def _descrizione_e_url(righe) -> tuple[str | None, str | None, list[str]]:
    urls: list[str] = []
    for org, sr in righe:
        if isinstance(org, dict):
            for k in ("url", "sameAs"):
                v = org.get(k)
                for u in (v if isinstance(v, list) else [v]):
                    if isinstance(u, str) and u.startswith("http") and u not in urls:
                        urls.append(u[:200])
            d = _pulito(org.get("description"))
            if len(d) >= 80:
                return d[:1500], "jsonld hiringOrganization", urls
        if isinstance(sr, str) and len(_pulito(sr)) >= 80:
            return _pulito(sr)[:1500], "smartrecruiters companyDescription", urls
    return None, None, urls


def applica(dsn: str, limite: int = 20000) -> dict:
    st = {"viste": 0, "fascia": 0, "sedi": 0, "descrizione": 0, "keywords": 0}
    t0 = time.time()
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute(DDL)
        tenants = c.execute(SQL_TENANT, (limite,)).fetchall()
        # i dati del registro/GLEIF con indirizzo, se un giorno ci saranno, entrano qui:
        # oggi ats_companies non ha colonne d'indirizzo, quindi hq_da e' sempre «offerte»
        for cid, e_reg, e_site, e_self, e_wd, band, paese in tenants:
            pid, slug = c.execute("SELECT platform_id, slug FROM ats_companies WHERE id = %s", (cid,)).fetchone()
            st["viste"] += 1
            for n, da in ((e_reg, "registro"), (e_site, "sito"), (e_self, "dichiarato"), (e_wd, "wikidata")):
                if n and int(n) > 0:
                    best, size_da = int(n), da
                    break
            else:
                best, size_da = None, None
            size = fascia(best) or (band if band else None)
            if size and not size_da:
                size_da = "registro (fascia)"
            sedi = c.execute(SQL_SEDI, (pid, slug)).fetchall()
            locs = [{"city": ci or None, "country": co or None, "state": s or None, "offerte": n, "is_primary": i == 0}
                    for i, (ci, co, s, n) in enumerate(sedi) if ci or co]
            hq = locs[0] if locs else None
            fam = [f for f, _ in c.execute(SQL_FAMIGLIE, (pid, slug)).fetchall()]
            try:
                tec = [t for t, _ in c.execute(SQL_TEC, (pid, slug)).fetchall()]
            except psycopg.errors.UndefinedTable:
                tec = []
            desc, desc_da, urls = _descrizione_e_url(c.execute(SQL_ORG, (pid, slug)).fetchall())
            kw = {"famiglie": fam, "tecnologie": tec} if (fam or tec) else None
            c.execute(SQL_SCRIVI, (cid, size, size_da, best, json.dumps(locs, ensure_ascii=False) if locs else None, len(locs),
                                   (hq or {}).get("country") or paese, (hq or {}).get("state"), (hq or {}).get("city"),
                                   None, None, None, "offerte" if hq else None,
                                   desc, desc_da, json.dumps(urls) if urls else None, json.dumps(kw, ensure_ascii=False) if kw else None))
            c.execute("UPDATE ats_companies SET dettagli_at = now() WHERE id = %s", (cid,))
            st["fascia"] += bool(size); st["sedi"] += bool(locs); st["descrizione"] += bool(desc); st["keywords"] += bool(kw)
            if st["viste"] % 500 == 0:
                log.info("aziende_dettagli: %s in %.0f s", st, time.time() - t0)
    return st


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=20000)
    a = ap.parse_args(argv)
    dsn = os.environ.get("ATS_DATABASE_URL")
    if not dsn:
        for f in ("/opt/nivult/.env", "/opt/nivult/engine/.env"):
            try:
                m = re.search(r"^POSTGRES_PASSWORD=(.*)$", open(f).read(), re.M)
                if m:
                    dsn = "postgresql://nivult:" + m.group(1).strip() + "@127.0.0.1:5432/nivult_ats"
                    break
            except OSError:
                pass
    print("Aziende dettagli:", applica(dsn, a.limite))
    return 0


if __name__ == "__main__":
    sys.exit(main())
