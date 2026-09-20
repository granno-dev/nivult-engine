"""Le tecnologie della scheda azienda vengono anche dalla testa v1, non solo dal 2B.

`costruisci_aziende.py` leggeva le tecnologie SOLO da `estrazioni_v2b`, cioe'
dal 2B — che e' spento dal 20/09. La testa v1 scrive in `tecnologie_v1`
(159.750 offerte vive al 20/09, ~390.000 al giorno) e niente di quello arrivava
nel prodotto: la colonna sarebbe rimasta ferma a quello che il 2B aveva fatto.

Due tabelle, due forme: il 2B scrive oggetti `{"nome","ruolo"}`, la testa
scrive stringhe. Qui si normalizzano entrambe a (azienda, offerta, nome) prima
di contare. Dove un'offerta ce l'hanno tutte e due (50.618), vince la testa v1:
richiamo piu' alto sullo stesso golden, ed e' quella che continuera' a scrivere.
"""
import pathlib, sys

P = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                 else "/opt/nivult/engine/scripts/costruisci_aziende.py")
s = P.read_text()

vecchio = """WITH lette AS (
  SELECT a.id AS cid, e.job_id, e.tecnologie
    FROM ats_companies a
    JOIN ats_jobs j ON j.platform_id = a.platform_id AND j.slug = a.slug
                   AND j.expired_at IS NULL
    JOIN estrazioni_v2b e ON e.job_id = j.id
   WHERE e.tecnologie IS NOT NULL AND e.modello NOT LIKE '%famiglia-esclusa%'
), contate AS (
  SELECT cid, t->>'nome' AS nome, count(DISTINCT job_id) AS offerte
    FROM lette CROSS JOIN LATERAL jsonb_array_elements(tecnologie) t
   WHERE coalesce(t->>'nome', '') <> ''
   GROUP BY 1, 2
), per_azienda AS ("""

nuovo = """WITH v1 AS (
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
), per_azienda AS ("""

assert vecchio in s, "la query delle tecnologie non combacia: guardare a mano"
s = s.replace(vecchio, nuovo)
s = s.replace("  2. le tecnologie aggregate per azienda, dalle estrazioni del 2B.",
              "  2. le tecnologie aggregate per azienda: dalla testa v1 (`tecnologie_v1`)\n"
              "     e, dove quella non e' passata, dal 2B (`estrazioni_v2b`).")
P.write_text(s)
print("patch applicata")
