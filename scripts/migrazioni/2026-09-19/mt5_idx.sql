-- Stesso ordine e stesso WHERE della query, o l'indice non viene usato: il 18/09
-- un (posted_at DESC) contro una query DESC NULLS LAST costava 131 secondi.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_mt5_coda_idx
  ON ats_jobs (posted_at DESC NULLS LAST)
  WHERE expired_at IS NULL AND sintesi_mt5_at IS NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_mt5_prenotate_idx
  ON ats_jobs (preso_mt5_at) WHERE preso_mt5_at IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS sintesi_mt5_ripasso_idx
  ON sintesi_mt5 (fiducia) WHERE ripassata_at IS NULL AND sintesi IS NOT NULL;
CREATE INDEX CONCURRENTLY IF NOT EXISTS sintesi_mt5_hash_idx ON sintesi_mt5 (testo_hash);
