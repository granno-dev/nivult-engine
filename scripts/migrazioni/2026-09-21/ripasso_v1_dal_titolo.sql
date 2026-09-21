-- 21/09/2026 — il ripasso di v1 sulle offerte decise dal solo titolo.
--
-- Le offerte delle piattaforme «testo dopo» (SmartRecruiters, Workday,
-- SuccessFactors, iCIMS, Breezy, BambooHR) classificate da v1 entro 2 ore
-- dalla creazione le ha viste, quasi sempre, senza descrizione. Misurato sul
-- N5 rileggendole col testo che c'e' adesso (400 a caso): la famiglia cambia
-- nel 10,1% dei casi (a confidenza >= 0,7), il contratto nel 7,5%, il remoto
-- nel 7,3%, la seniority nel 2,1%. Stima della popolazione: ~800.000.
--
-- TRE PASSI, NELL'ORDINE, e il secondo NON e' SQL:
--   1. questo file, prima parte: si azzerano seniority / employment_type /
--      remote e dichiarati_at su quelle offerte (i valori dichiarati dal
--      recruiter tornano al passo 2; quelli di v1 dal titolo spariscono)
--   2. `python -m nivult.ats.dichiarati --limite 1000000`: rimette i valori
--      DICHIARATI (esatti, gratis) PRIMA che v1 ripassi — se v1 arrivasse
--      prima, coalesce terrebbe la sua stima e il dichiarato andrebbe perso
--   3. questo file, seconda parte (\i con -v passo=2): via le famiglie
--      scritte da v1 e locale_v1_at a NULL, cosi' il demone le rilegge col
--      testo. Le famiglie di GLM restano: v1 ci riscrive solo il parere.
-- Idempotente: la tabella d'appoggio si ricrea a ogni esecuzione.
SET statement_timeout = '60min';

CREATE TABLE IF NOT EXISTS ripasso_v1_dal_titolo (job_id uuid PRIMARY KEY, creato_at timestamptz DEFAULT now());
INSERT INTO ripasso_v1_dal_titolo (job_id)
SELECT j.id
  FROM ats_jobs j
 WHERE j.expired_at IS NULL AND j.locale_v1_at IS NOT NULL
   AND j.locale_v1_at < j.created_at + interval '2 hours'
   AND j.platform_id IN ('smartrecruiters','workday','successfactors','icims','breezy','bamboohr')
   AND EXISTS (SELECT 1 FROM unnest(ARRAY[j.raw->>'description', j.raw->>'content', j.raw->>'descriptionHtml',
                 j.raw->>'descriptionPlain', j.raw->>'externalDescription', j.raw->>'jobDescription',
                 j.raw->>'job_description', j.raw->>'Job_Description', j.raw->>'body', j.raw->>'content_html',
                 j.raw->>'description_html', j.raw->>'descriptionBody', j.raw->>'text',
                 j.raw->'_jobposting'->>'description', j.raw->>'ShortDescriptionStr']) v WHERE length(v) >= 300)
ON CONFLICT DO NOTHING;
SELECT count(*) AS da_ripassare FROM ripasso_v1_dal_titolo;

-- passo 1: via i campi stimati dal titolo (torneranno: dichiarati al passo 2, v1 al passo 3)
UPDATE ats_jobs j SET seniority = NULL, employment_type = NULL, remote = NULL, dichiarati_at = NULL
  FROM ripasso_v1_dal_titolo r WHERE j.id = r.job_id AND j.locale_v1_at IS NOT NULL;
