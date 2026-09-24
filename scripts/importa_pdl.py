"""Import del match People Data Labs Free Company Dataset in ats_companies.

Il 24/09/2026 il join sui 45.478 domini venduti (coalesce(site_domain,
logo_domain)) ha dato 35.942 match (79%): settore, fascia dipendenti,
fondata, sede e linkedin_url per meta' del parco aziende, a costo zero,
CC BY 4.0 (attribuzione: People Data Labs Free Company Dataset).

Ogni fonte la sua colonna, come da regola 4: i campi arrivano con il
prefisso pdl_ e pdl_at marca il giro. Le gerarchie (industry, size_range,
dipendenti) li leggono come fonte «pdl (free dataset)» al giro successivo
di aziende_dettagli — non si mescola MAI con registro o sito.

I 15 domini-piattaforma/franchise dove PDL attribuisce al dominio madre
migliaia di sotto-entita' (misura del 24/09: yelp, twitch, samsung...)
restano fuori: match univoco impossibile, si gestiranno a mano.

Gira sul server: ATS_DATABASE_URL=... python scripts/importa_pdl.py pdl_match_full.jsonl
"""
import json
import os
import sys

import psycopg

# i domini-piattaforma esclusi (misura PDL del 24/09: sotto-entita' a
# migliaia sul dominio madre, match univoco impossibile)
PIATTAFORME = {"bloomberg.com", "casa.it", "deloitte.com", "fraunhofer.de",
               "hilton.com", "kpmg.com", "pinterest.com", "redcross.org",
               "samsung.com", "siemens.com", "twitch.tv", "upm.es",
               "visitingangels.com", "yelp.com", "zomato.com"}

COLONNE = (("pdl_name", "text"), ("pdl_industry", "text"), ("pdl_size", "text"),
           ("pdl_founded", "text"), ("pdl_locality", "text"), ("pdl_region", "text"),
           ("pdl_country", "text"), ("pdl_linkedin_url", "text"), ("pdl_at", "timestamptz"))


def main() -> None:
    percorso = sys.argv[1] if len(sys.argv) > 1 else "/tmp/pdl_match_full.jsonl"
    dsn = os.environ["ATS_DATABASE_URL"]
    righe = []
    for r in map(json.loads, open(percorso, encoding="utf-8")):
        if r["dominio"] in PIATTAFORME:
            continue
        righe.append((r["dominio"], r.get("name") or None, r.get("industry") or None,
                      r.get("size") or None, r.get("founded") or None,
                      r.get("locality") or None, r.get("region") or None,
                      r.get("country") or None, r.get("linkedin_url") or None))
    print(f"righe da importare: {len(righe)}")
    with psycopg.connect(dsn) as c:
        # la guardia anti-lock: ALTER solo se manca, con lock_timeout
        # (la trappola del 21/09: ADD COLUMN IF NOT EXISTS prende il lock
        # esclusivo anche quando la colonna c'e' gia')
        presenti = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'ats_companies'")}
        mancanti = [(n, t) for n, t in COLONNE if n not in presenti]
        if mancanti:
            c.execute("SET lock_timeout = '10s'")
            for n, t in mancanti:
                c.execute(f"ALTER TABLE ats_companies ADD COLUMN {n} {t}")
            c.execute("RESET lock_timeout")
            print(f"colonne aggiunte: {[n for n, _ in mancanti]}")
        c.execute("CREATE TEMP TABLE pdl_tmp (dominio text, name text, industry text, "
                  "size text, founded text, locality text, region text, country text, "
                  "linkedin_url text) ON COMMIT DROP")
        with c.cursor() as cur, cur.copy("COPY pdl_tmp FROM STDIN") as copy:
            for r in righe:
                copy.write_row(r)
        n = c.execute("""
            UPDATE ats_companies c
               SET pdl_name = t.name, pdl_industry = t.industry, pdl_size = t.size,
                   pdl_founded = t.founded, pdl_locality = t.locality,
                   pdl_region = t.region, pdl_country = t.country,
                   pdl_linkedin_url = t.linkedin_url, pdl_at = now()
              FROM pdl_tmp t
             WHERE lower(coalesce(c.site_domain, c.logo_domain)) = t.dominio""").rowcount
        c.commit()
        print(f"tenant aggiornati: {n}")


if __name__ == "__main__":
    main()
