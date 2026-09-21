-- 21/09/2026 — passo 3 del ripasso (vedi ripasso_v1_dal_titolo.sql): si esegue
-- DOPO `python -m nivult.ats.dichiarati`. Via le famiglie che v1 aveva scritto
-- dal solo titolo, e l'offerta torna in coda a v1 col testo.
SET statement_timeout = '60min';
DELETE FROM job_classifications x
 USING ripasso_v1_dal_titolo r
 WHERE x.job_id = r.job_id AND x.model = 'nivult-v1';
UPDATE ats_jobs j SET locale_v1_at = NULL
  FROM ripasso_v1_dal_titolo r WHERE j.id = r.job_id;
SELECT count(*) AS in_coda_a_v1 FROM ats_jobs j JOIN ripasso_v1_dal_titolo r ON r.job_id = j.id WHERE j.locale_v1_at IS NULL;
