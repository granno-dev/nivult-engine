"""La resa e' passata dal 23% al 92%. E' vera o stiamo scrivendo spazzatura?

Tre controlli, nell'ordine in cui smentirebbero il risultato:
  1. a che LIVELLO sono stati scritti: se sono quasi tutti livello 2, il salto
     viene dall'aver abbassato l'asticella, non dall'aver cercato meglio;
  2. quanto il dominio somiglia al nome: sui livello 1 dev'essere alto ma non
     sempre (Orbotech -> kla.com), sui livello 2 dev'essere >= 0,80 per costruzione;
  3. il controllo che conta davvero — un dominio rivendicato da piu' aziende e'
     il segnale di allarme che stamattina ha scoperto rippling.com.
"""
import os
import re

import psycopg

with psycopg.connect(os.environ["ATS_DATABASE_URL"]) as c:
    def n(s, *p):
        return c.execute(s, p).fetchone()[0]

    print("=== 1. a che livello sono stati scritti (ultima ora) ===")
    for liv, q in c.execute("""
        SELECT coalesce(site_domain_livello, 0), count(*) FROM ats_companies
         WHERE site_checked_at > now() - '60 minutes'::interval AND site_domain IS NOT NULL
         GROUP BY 1 ORDER BY 1"""):
        etichetta = {1: "livello 1 (rimanda al nostro tenant)",
                     2: "livello 2 (corrisponde al nome)",
                     0: "SENZA LIVELLO (scritti prima della modifica)"}[liv]
        print(f"  {etichetta:<46}{q:>7,}")

    print("\n=== 2. per fonte ===")
    for f, q in c.execute("""
        SELECT site_domain_source, count(*) FROM ats_companies
         WHERE site_checked_at > now() - '60 minutes'::interval AND site_domain IS NOT NULL
         GROUP BY 1 ORDER BY 2 DESC"""):
        print(f"  {f or '?':<24}{q:>7,}")

    print("\n=== 3. IL CAMPANELLO: domini rivendicati da piu' aziende ===")
    for dom, q, chi in c.execute("""
        SELECT site_domain, count(*), string_agg(DISTINCT company_name, ' | ')
          FROM ats_companies WHERE site_domain IS NOT NULL
         GROUP BY 1 HAVING count(*) > 2 ORDER BY 2 DESC LIMIT 8"""):
        print(f"  {dom:<32}{q:>4}  {(chi or '')[:52]}")
    tot = n("""SELECT count(*) FROM ats_companies a WHERE a.site_domain IN
                 (SELECT site_domain FROM ats_companies WHERE site_domain IS NOT NULL
                   GROUP BY 1 HAVING count(*) > 2)""")
    print(f"  righe su domini condivisi da piu' di 2 aziende: {tot:,}")

    print("\n=== 4. campione dei nuovi, con nome e livello ===")
    for nome, dom, liv, fonte in c.execute("""
        SELECT company_name, site_domain, site_domain_livello, site_domain_source
          FROM ats_companies
         WHERE site_checked_at > now() - '60 minutes'::interval AND site_domain IS NOT NULL
         ORDER BY random() LIMIT 16"""):
        print(f"  liv{liv or '?'}  {(nome or '?')[:30]:<32}{dom[:34]:<36}{fonte}")
