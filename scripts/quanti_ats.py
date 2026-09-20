"""Quanti dei domini «trovati» sono in realta' la bacheca stessa?

Il giudice chiede che il sito rimandi al nostro tenant ATS. Se il candidato E' la
bacheca — `srvmedia.zohorecruit.in` — la prova passa per forza: sta rimandando a
se stesso. E' rippling.com in forma nuova, e la causa e' la stessa: `zohorecruit`
non e' nella lista degli host che non sono aziende.

Si misura su tutto l'archivio, non solo sugli ultimi: il buco potrebbe essere
vecchio.
"""
import os
import re

import psycopg

# tutti gli ATS noti, compresi quelli che mancavano
ATS = (r"zohorecruit|recruitee|workable|lever\.co|greenhouse|smartrecruiters|personio|"
       r"ashby|teamtailor|myworkday|workday|icims|successfactors|taleo|jobvite|bamboohr|"
       r"rippling|applytojob|breezy|recruiterbox|jazzhr|jazz\.co|catsone|vincere|"
       r"cornerstone|csod|homerun|freshteam|comeet|pinpointhq|join\.com|softgarden|"
       r"heavenhr|jobsoid|traffit|taleez|trakstar|hirehive|jobscore|applicantstack|"
       r"niceboard|crelate|hiringthing|eightfold|avature|phenom|pageup|radancy|"
       r"jibeapply|smartjobboard|werecruit|digitalrecruiters|dvinci|talentsoft|"
       r"careerpuck|cloud\.sap|sapsf|oraclecloud|adp\.com|paylocity|paycom|paycor|ukg")

with psycopg.connect(os.environ["ATS_DATABASE_URL"]) as c:
    tot = c.execute("SELECT count(*) FROM ats_companies WHERE site_domain IS NOT NULL").fetchone()[0]
    ats = c.execute("SELECT count(*) FROM ats_companies WHERE site_domain IS NOT NULL AND site_domain ~* %s",
                    (ATS,)).fetchone()[0]
    print(f"domini in archivio: {tot:,}")
    print(f"  che sono in realta' una BACHECA: {ats:,}  ({100*ats/max(tot,1):.1f}%)\n")

    print("quali, e quanti:")
    for dom, q in c.execute("""
        SELECT regexp_replace(site_domain, '^[^.]+\\.', ''), count(*)
          FROM ats_companies WHERE site_domain IS NOT NULL AND site_domain ~* %s
         GROUP BY 1 ORDER BY 2 DESC LIMIT 12""", (ATS,)):
        print(f"  {dom:<34}{q:>7,}")

    print("\nquando sono stati scritti:")
    for quando, q in c.execute("""
        SELECT CASE WHEN site_checked_at > now() - '2 hours'::interval THEN 'nelle ultime 2 ore'
                    ELSE 'prima' END, count(*)
          FROM ats_companies WHERE site_domain IS NOT NULL AND site_domain ~* %s
         GROUP BY 1 ORDER BY 2 DESC""", (ATS,)):
        print(f"  {quando:<24}{q:>7,}")

    print("\nla resa VERA delle ultime due ore, tolte le bacheche:")
    tutti = c.execute("""SELECT count(*) FROM ats_companies
                          WHERE site_checked_at > now() - '2 hours'::interval
                            AND site_domain IS NOT NULL""").fetchone()[0]
    puliti = c.execute("""SELECT count(*) FROM ats_companies
                           WHERE site_checked_at > now() - '2 hours'::interval
                             AND site_domain IS NOT NULL AND site_domain !~* %s""", (ATS,)).fetchone()[0]
    print(f"  scritti {tutti:,}, di cui veri {puliti:,}  ({100*puliti/max(tutti,1):.0f}%)")
