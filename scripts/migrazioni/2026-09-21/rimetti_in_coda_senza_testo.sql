-- 21/09/2026 — la gara col dettaglio.
-- Il demone mT5 marcava «saltato:senza-testo» le offerte lette prima che il
-- passo di dettaglio portasse la descrizione, e non ci tornava piu'. Qui le
-- righe il cui annuncio ORA ha un testo (>= 300 caratteri in uno dei campi che
-- il demone legge) tornano in coda: si toglie il segnaposto e si azzera il
-- timbro. Il demone, con la regola nuova, le riprende solo se il testo c'e'.
-- Idempotente: una seconda esecuzione non trova piu' niente.
SET statement_timeout = '30min';
BEGIN;
CREATE TEMP TABLE da_rifare AS
SELECT s.job_id
  FROM sintesi_mt5 s JOIN ats_jobs j ON j.id = s.job_id
 WHERE s.modello LIKE '%saltato:senza-testo%'
   AND j.expired_at IS NULL
   AND EXISTS (SELECT 1 FROM unnest(ARRAY[j.raw->>'description', j.raw->>'content', j.raw->>'descriptionHtml',
                 j.raw->>'descriptionPlain', j.raw->>'externalDescription', j.raw->>'jobDescription',
                 j.raw->>'job_description', j.raw->>'Job_Description', j.raw->>'body', j.raw->>'content_html',
                 j.raw->>'description_html', j.raw->>'descriptionBody', j.raw->>'text',
                 j.raw->>'ShortDescriptionStr']) v WHERE length(v) >= 300);
SELECT count(*) AS rimesse_in_coda FROM da_rifare;
DELETE FROM sintesi_mt5 WHERE job_id IN (SELECT job_id FROM da_rifare);
UPDATE ats_jobs SET sintesi_mt5_at = NULL, preso_mt5_at = NULL WHERE id IN (SELECT job_id FROM da_rifare);
COMMIT;
