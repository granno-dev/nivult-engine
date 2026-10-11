-- 11/10/2026 — il cruscotto non scansiona piu' 4M righe a ogni ricalcolo.
--
-- Scoperto il giorno del fermo: dall'8/10 il pannello pesante del cruscotto
-- serviva la cache su disco perche' DUE query superavano il loro timeout —
-- l'anti-join delle non-classificate (76 s misurati, tetto 60) e la
-- copertura campi (262 s gia' il 26/09, oltre 300 sotto il carico di oggi).
-- I numeri non cambiano in minuti: li misura la notte, li legge il giorno.
--
-- Viste materializzate a riga singola, rinfrescate da deploy/rinfresca-viste.sh.
-- L'indice "unico" finto serve al REFRESH CONCURRENTLY, che non blocca i
-- lettori: il cruscotto legge mentre la notte ricalcola.

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_cruscotto_copertura AS
SELECT count(country) AS paese,
       count(salary_min) AS salario,
       count(seniority) AS seniority,
       count(remote) AS remoto,
       count(*) FILTER (WHERE skills IS NOT NULL
                            AND array_length(skills, 1) > 0) AS competenze,
       count(*) FILTER (WHERE raw ?| array['description',
           'descriptionHtml','jobDescription','job_description',
           'content','externalDescription','descriptionPlain']) AS descrizione,
       count(employment_type) AS contratto,
       count(contact_email) AS contatto,
       count(lang) AS lingua,
       count(*) FILTER (WHERE languages_required IS NOT NULL
                            AND array_length(languages_required, 1) > 0) AS lingue
  FROM ats_jobs WHERE expired_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS mv_cruscotto_copertura_uq
  ON mv_cruscotto_copertura (paese);

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_cruscotto_non_class AS
SELECT count(*) AS n
  FROM ats_jobs j
 WHERE j.expired_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM job_classifications c WHERE c.job_id = j.id);
CREATE UNIQUE INDEX IF NOT EXISTS mv_cruscotto_non_class_uq
  ON mv_cruscotto_non_class (n);
