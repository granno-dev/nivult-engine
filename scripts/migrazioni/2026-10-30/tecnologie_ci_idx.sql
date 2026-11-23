CREATE INDEX CONCURRENTLY IF NOT EXISTS tecnologie_v1_tecnologie_ci_gin
  ON tecnologie_v1 USING gin ((lower(tecnologie::text)::jsonb));
