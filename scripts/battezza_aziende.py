"""Da' un nome alle aziende che non ce l'hanno — ma solo dove c'e' una prova.

Il nome si prende da `hiringOrganization.name` nel JSON-LD dell'annuncio: e'
l'azienda che dichiara se stessa, non una nostra deduzione. Lo slug NON si usa:
produrrebbe «Tgocorp» e «Yeweyewe Com» spacciati per ragioni sociali.

LA CONDIZIONE CHE FA TUTTO IL LAVORO: si battezza solo il tenant che dichiara UN
SOLO nome in tutte le sue offerte. Un tenant che ne dichiara 769 — mindpal.co —
non e' un'azienda: e' una bacheca, e prendendo il primo nome le daremmo quello di
un suo cliente. Misurato il 19/09/2026: 1.051 tenant con un solo nome,
429 con ventuno o piu'.

  ATS_DATABASE_URL=... python battezza_aziende.py [--dry-run]
"""
from __future__ import annotations
import argparse, os, sys
import psycopg

DICHIARATO = """coalesce(j.raw #>> '{hiringOrganization,name}',
                         j.raw #>> '{company,name}',
                         j.raw ->> 'companyName',
                         j.raw ->> 'company_name')"""

TROVA = f"""
WITH candidati AS (
  SELECT a.id,
         count(DISTINCT {DICHIARATO}) AS quanti,
         min({DICHIARATO})            AS nome
    FROM ats_companies a
    JOIN ats_jobs j ON j.platform_id = a.platform_id AND j.slug = a.slug
                   AND j.expired_at IS NULL
   WHERE (a.company_name IS NULL OR a.company_name = '')
     AND {DICHIARATO} IS NOT NULL
   GROUP BY 1)
SELECT id, nome FROM candidati
 WHERE quanti = 1
   AND length(btrim(nome)) BETWEEN 2 AND 120
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
        righe = c.execute(TROVA).fetchall()
        print(f"aziende battezzabili senza rischio: {len(righe):,}")
        for _, nome in righe[:10]:
            print(f"    {nome[:60]}")
        if a.dry_run:
            print("(prova a secco: non ho scritto niente)")
            return 0
        with c.cursor() as cur:
            cur.executemany(
                "UPDATE ats_companies SET company_name = %s, company_name_source = 'dichiarato' "
                "WHERE id = %s AND (company_name IS NULL OR company_name = '')",
                [(n.strip(), i) for i, n in righe])
        print(f"scritti {len(righe):,} nomi (fonte: dichiarato)")
        resta = c.execute("""
            SELECT count(*) FROM ats_companies a
             WHERE (a.company_name IS NULL OR a.company_name = '')
               AND EXISTS (SELECT 1 FROM ats_jobs j
                            WHERE j.platform_id = a.platform_id AND j.slug = a.slug
                              AND j.expired_at IS NULL)""").fetchone()[0]
        print(f"restano senza nome: {resta:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
