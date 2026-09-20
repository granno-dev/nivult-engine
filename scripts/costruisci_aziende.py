"""Ricostruisce `aziende_vendibili`: la forma in cui il mercato compra.

Due passaggi, perche' uno solo sarebbe una query da minuti di cui non si vede
niente finche' non finisce:
  1. l'anagrafica + il conteggio delle offerte attive;
  2. le tecnologie aggregate per azienda: dalla testa v1 (`tecnologie_v1`)
     e, dove quella non e' passata, dal 2B (`estrazioni_v2b`).

L'aggancio fra offerta e azienda e' (platform_id, slug): `ats_jobs` non ha un
company_id. Copre il 95,1% delle offerte attive.

  ATS_DATABASE_URL=... python costruisci_aziende.py [--solo-tecnologie]
"""
from __future__ import annotations
import argparse, os, sys, time
import psycopg

DICHIARATO = """coalesce(j.raw #>> '{hiringOrganization,name}',
                         j.raw #>> '{company,name}',
                         j.raw ->> 'companyName',
                         j.raw ->> 'company_name')"""

# BACHECA O DATORE. Un tenant che dichiara 769 nomi di aziende diverse nelle sue
# offerte — mindpal.co — non e' un datore: e' una bacheca, e nel prodotto non ci
# va. La prova e' il numero di nomi DISTINTI dichiarati sotto lo stesso tenant:
# un'azienda ne dichiara uno, una bacheca centinaia. Misurato il 19/09/2026:
# 429 tenant con 21+ nomi, che da soli tengono 73.162 offerte attive.
BACHECHE = f"""
WITH nomi AS (
  SELECT a.id, count(DISTINCT {{d}}) AS quanti
    FROM ats_companies a
    JOIN ats_jobs j ON j.platform_id = a.platform_id AND j.slug = a.slug
                   AND j.expired_at IS NULL
   WHERE {{d}} IS NOT NULL
   GROUP BY 1)
UPDATE aziende_vendibili v
   SET nomi_dichiarati = n.quanti,
       e_bacheca = (n.quanti >= %s)
  FROM nomi n WHERE v.company_id = n.id
"""

# DOPPIONI. Piu' tenant della stessa azienda: «1pact» ne ha 8 regionali. Non si
# cancella niente — si elegge un capofila e gli altri lo indicano, cosi' chi
# compra puo' chiedere una riga per azienda senza che noi perdiamo un tenant.
# Due prove, di forza diversa:
#   - stesso DOMINIO: e' una prova (entrambi hanno dimostrato di stare su quel sito);
#   - stesso NOME normalizzato E stesso paese: e' un indizio forte.
# Capofila = quello con piu' offerte attive, a parita' il piu' vecchio.
DOPPIONI = """
WITH chiavi AS (
  SELECT company_id, 'dom:' || dominio AS k FROM aziende_vendibili
   WHERE dominio IS NOT NULL AND NOT e_bacheca
  UNION ALL
  SELECT company_id, 'nom:' || lower(regexp_replace(nome, '[^a-zA-Z0-9]', '', 'g'))
                            || ':' || coalesce(paese, '')
    FROM aziende_vendibili
   WHERE nome IS NOT NULL AND NOT e_bacheca
     AND length(regexp_replace(nome, '[^a-zA-Z0-9]', '', 'g')) >= 4
), gruppi AS (
  -- Il capofila deve essere VENDIBILE, o tutto il gruppo sparisce dal prodotto:
  -- gli altri puntano a lui, e se lui e' senza nome o e' una bacheca nessuno del
  -- gruppo entra in `aziende_pronte`. Succedeva a 10 gruppi (19/09/2026).
  -- Quindi prima chi ha un nome e non e' bacheca, poi chi ha piu' offerte.
  SELECT k, company_id,
         first_value(company_id) OVER (
           PARTITION BY k ORDER BY (nome IS NOT NULL) DESC, e_bacheca,
                                   offerte_attive DESC, aggiornato_at, company_id) AS capo
    FROM chiavi JOIN aziende_vendibili USING (company_id)
), scelta AS (
  -- un tenant puo' cadere in due chiavi (dominio e nome): vince la prima in ordine,
  -- cosi' il risultato non dipende da come il database restituisce le righe
  SELECT DISTINCT ON (company_id) company_id, capo
    FROM gruppi ORDER BY company_id, k
)
UPDATE aziende_vendibili v SET canonico_id = s.capo
  FROM scelta s WHERE v.company_id = s.company_id
"""

ANAGRAFICA = """
INSERT INTO aziende_vendibili
  (company_id, nome, piattaforma, slug, dominio, dominio_fonte,
   paese, settore, dipendenti, lei, offerte_attive, ultima_offerta, nome_fonte,
   dominio_livello, aggiornato_at)
SELECT a.id, a.company_name, p.name, a.slug,
       a.site_domain, a.site_domain_source,
       coalesce(a.country, v.paese),
       coalesce(a.industry, a.industry_reg, a.industry_site),
       -- ogni fonte ha il suo tipo: una e' una fascia («50-200»), le altre numeri.
       -- Senza il cast esplicito Postgres rifiuta il coalesce fra testo e intero.
       coalesce(a.employees_reg_band::text, a.employees_reg::text, a.employees_self::text,
                a.employees_site::text, a.employees_wd::text),
       a.lei,
       coalesce(v.n, 0), v.ultima, a.company_name_source,
       a.site_domain_livello, now()
  FROM ats_companies a
  JOIN ats_platforms p ON p.id = a.platform_id
  LEFT JOIN LATERAL (
        SELECT count(*) AS n, max(j.posted_at) AS ultima,
               mode() WITHIN GROUP (ORDER BY j.country) AS paese
          FROM ats_jobs j
         WHERE j.platform_id = a.platform_id AND j.slug = a.slug
           AND j.expired_at IS NULL) v ON true
 WHERE coalesce(v.n, 0) > 0
ON CONFLICT (company_id) DO UPDATE SET
  nome = EXCLUDED.nome, piattaforma = EXCLUDED.piattaforma, slug = EXCLUDED.slug,
  dominio = EXCLUDED.dominio, dominio_fonte = EXCLUDED.dominio_fonte,
  paese = EXCLUDED.paese, settore = EXCLUDED.settore,
  dipendenti = EXCLUDED.dipendenti, lei = EXCLUDED.lei,
  offerte_attive = EXCLUDED.offerte_attive, ultima_offerta = EXCLUDED.ultima_offerta,
  nome_fonte = EXCLUDED.nome_fonte, dominio_livello = EXCLUDED.dominio_livello,
  aggiornato_at = now()
"""

# Le tecnologie di un'azienda sono quelle citate nelle sue offerte VIVE, contate per
# quante offerte le nominano: una citata in venti annunci e' il suo stack, una citata
# una volta puo' essere un di piu'. Si escludono le righe scritte dal buttafuori
# (`+famiglia-esclusa`): quella lista vuota l'ha decisa una regola, non il modello,
# e non deve pesare sul conteggio delle offerte lette.
TECNOLOGIE = """
WITH v1 AS (
  -- la testa v1: array di stringhe. LEFT JOIN LATERAL, cosi' un'offerta letta
  -- e trovata vuota conta fra le lette (vale zero, non «non so»)
  SELECT a.id AS cid, t.job_id, x AS nome
    FROM ats_companies a
    JOIN ats_jobs j ON j.platform_id = a.platform_id AND j.slug = a.slug
                   AND j.expired_at IS NULL
    JOIN tecnologie_v1 t ON t.job_id = j.id
    LEFT JOIN LATERAL jsonb_array_elements_text(t.tecnologie) x ON true
), v2b AS (
  -- il 2B: array di oggetti {nome, ruolo}. Solo dove la testa v1 non e' passata:
  -- dove ci sono tutte e due vince v1 (richiamo piu' alto sullo stesso golden)
  SELECT a.id AS cid, e.job_id, el->>'nome' AS nome
    FROM ats_companies a
    JOIN ats_jobs j ON j.platform_id = a.platform_id AND j.slug = a.slug
                   AND j.expired_at IS NULL
    JOIN estrazioni_v2b e ON e.job_id = j.id
    LEFT JOIN LATERAL jsonb_array_elements(e.tecnologie) el ON true
   WHERE e.tecnologie IS NOT NULL AND e.modello NOT LIKE '%famiglia-esclusa%'
     AND NOT EXISTS (SELECT 1 FROM tecnologie_v1 t WHERE t.job_id = e.job_id)
), lette AS (
  SELECT * FROM v1 UNION ALL SELECT * FROM v2b
), contate AS (
  SELECT cid, nome, count(DISTINCT job_id) AS offerte
    FROM lette
   WHERE coalesce(nome, '') <> ''
   GROUP BY 1, 2
), per_azienda AS (
  SELECT cid,
         jsonb_agg(jsonb_build_object('nome', nome, 'offerte', offerte)
                   ORDER BY offerte DESC, nome) AS tec,
         count(*) AS n
    FROM contate GROUP BY 1
), quante AS (
  SELECT cid, count(DISTINCT job_id) AS lette FROM lette GROUP BY 1
)
UPDATE aziende_vendibili v
   -- un'azienda le cui offerte il 2B ha letto SENZA trovare tecnologie non ha
   -- riga in per_azienda: vale zero, non «non so». La differenza fra le due la
   -- dice offerte_lette, che resta > 0.
   SET tecnologie = p.tec, n_tecnologie = coalesce(p.n, 0),
       offerte_lette = coalesce(q.lette, 0), aggiornato_at = now()
  FROM quante q LEFT JOIN per_azienda p ON p.cid = q.cid
 WHERE v.company_id = q.cid
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--solo-tecnologie", action="store_true")
    ap.add_argument("--bacheche", action="store_true",
                    help="rifa\' la rilevazione delle bacheche: lenta, non serve ogni giorno")
    ap.add_argument("--soglia-bacheca", type=int, default=21,
                    help="da quanti nomi di datori diversi in su il tenant e' una bacheca")
    a = ap.parse_args()
    with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
        if not a.solo_tecnologie:
            t = time.time()
            n = c.execute(ANAGRAFICA).rowcount
            print(f"anagrafica: {n:,} aziende in {time.time()-t:.0f}s", flush=True)
        # Un'azienda che ha perso tutte le offerte esce dalla tabella. L'INSERT
        # sopra tocca solo chi ne ha ancora, quindi da solo lascerebbe righe vecchie
        # con un conteggio che non e' piu' vero — e su un dato che si vende una
        # riga stantia e' peggio di una riga assente.
        via = c.execute("""
            DELETE FROM aziende_vendibili v
             WHERE NOT EXISTS (
               SELECT 1 FROM ats_companies a
                 JOIN ats_jobs j ON j.platform_id = a.platform_id AND j.slug = a.slug
                                AND j.expired_at IS NULL
                WHERE a.id = v.company_id)""").rowcount
        print(f"tolte:      {via:,} aziende senza piu' offerte attive", flush=True)

        t = time.time()
        n = c.execute(TECNOLOGIE).rowcount
        print(f"tecnologie: {n:,} aziende aggiornate in {time.time()-t:.0f}s", flush=True)

        if a.bacheche:
            t = time.time()
            # La query ha UN segnaposto, la soglia. Il secondo parametro (una lista di
            # piattaforme mai definita) era un resto di una modifica mai finita:
            # NameError a ogni --bacheche, gia' nel repo (20/09/2026).
            c.execute(BACHECHE.format(d=DICHIARATO), (a.soglia_bacheca,))
            b = c.execute("SELECT count(*) FROM aziende_vendibili WHERE e_bacheca").fetchone()[0]
            off = c.execute("SELECT coalesce(sum(offerte_attive), 0) FROM aziende_vendibili WHERE e_bacheca").fetchone()[0]
            print(f"bacheche:   {b:,} tenant marcati come bacheca ({off:,} offerte) in {time.time()-t:.0f}s", flush=True)

        t = time.time()
        c.execute("UPDATE aziende_vendibili SET canonico_id = NULL")
        c.execute(DOPPIONI)
        # CATENE. Un tenant puo' cadere in due chiavi (dominio e nome) e seguirne
        # una sola: cosi' A indica B, ma B indica C — e il gruppo di A sparisce dal
        # prodotto perche' B non e' un capofila. Succedeva a 3 gruppi (19/09/2026).
        # Si segue la catena finche' non si ferma; il limite di 10 e' una rete di
        # sicurezza contro un anello, non un numero atteso.
        for giro in range(10):
            mossi = c.execute("""
                UPDATE aziende_vendibili v SET canonico_id = capo.canonico_id
                  FROM aziende_vendibili capo
                 WHERE v.canonico_id = capo.company_id
                   AND capo.canonico_id IS NOT NULL
                   AND capo.canonico_id <> capo.company_id""").rowcount
            if not mossi:
                break
        else:
            print("ATTENZIONE: catene di capofila non risolte in 10 giri", flush=True)
        d = c.execute("SELECT count(*) FROM aziende_vendibili WHERE canonico_id IS NOT NULL AND canonico_id <> company_id").fetchone()[0]
        print(f"doppioni:   {d:,} righe assorbite da un capofila in {time.time()-t:.0f}s", flush=True)
        for k, s in [
            ("aziende con offerte attive", "SELECT count(*) FROM aziende_vendibili"),
            ("  con un dominio",           "SELECT count(*) FROM aziende_vendibili WHERE dominio IS NOT NULL"),
            ("  con almeno una tecnologia","SELECT count(*) FROM aziende_vendibili WHERE n_tecnologie > 0"),
            ("  con dominio E tecnologie", "SELECT count(*) FROM aziende_vendibili WHERE dominio IS NOT NULL AND n_tecnologie > 0"),
            ("  con un settore",           "SELECT count(*) FROM aziende_vendibili WHERE settore IS NOT NULL"),
            ("  con i dipendenti",         "SELECT count(*) FROM aziende_vendibili WHERE dipendenti IS NOT NULL"),
        ]:
            print(f"{k:<30}{c.execute(s).fetchone()[0]:>10,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
