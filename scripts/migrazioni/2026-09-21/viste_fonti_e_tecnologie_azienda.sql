-- 21/09/2026 — due campi di Coresignal che avevamo gia' nei dati e non esponevamo.
--
-- 1. job_sources: la stessa offerta vista su piu' fonti. Il gemellaggio esiste
--    (ats_jobs.duplicate_key, ~27% delle attive); qui diventa, per ogni offerta,
--    l'elenco delle altre copie con piattaforma, url e stato.
CREATE OR REPLACE VIEW offerte_fonti AS
SELECT j.id AS job_id, j.duplicate_key,
       jsonb_agg(jsonb_build_object('job_id', k.id, 'platform', k.platform_id, 'slug', k.slug, 'url', k.url,
                                    'posted_at', k.posted_at, 'status', CASE WHEN k.expired_at IS NULL THEN 'active' ELSE 'expired' END)
                 ORDER BY k.posted_at DESC NULLS LAST) AS job_sources,
       count(*) AS n_fonti
  FROM ats_jobs j JOIN ats_jobs k ON k.duplicate_key = j.duplicate_key AND k.id <> j.id
 WHERE j.duplicate_key IS NOT NULL
 GROUP BY j.id, j.duplicate_key;

-- 2. azienda_tecnologie: le tecnologie DELL'AZIENDA con prima e ultima data in
--    cui sono comparse in un suo annuncio (Coresignal: company_technologies con
--    first_verified_at / last_verified_at). Viene dalla testa tecnologie per
--    annuncio (tecnologie_v1): il tenant e' (platform_id, slug). Materializzata,
--    perche' l'aggregazione su 2,7M offerte non si fa a ogni lettura; il cron la
--    rinfresca ogni notte.
CREATE MATERIALIZED VIEW IF NOT EXISTS azienda_tecnologie AS
SELECT j.platform_id, j.slug, t.nome AS technology,
       count(*) AS annunci, min(j.posted_at) AS first_verified_at, max(j.posted_at) AS last_verified_at,
       count(*) FILTER (WHERE j.expired_at IS NULL) AS annunci_attivi
  FROM tecnologie_v1 v JOIN ats_jobs j ON j.id = v.job_id,
       LATERAL jsonb_array_elements_text(v.tecnologie) AS t(nome)
 GROUP BY 1, 2, 3;
CREATE UNIQUE INDEX IF NOT EXISTS azienda_tecnologie_idx ON azienda_tecnologie (platform_id, slug, technology);
-- rinfresco: REFRESH MATERIALIZED VIEW CONCURRENTLY azienda_tecnologie;
