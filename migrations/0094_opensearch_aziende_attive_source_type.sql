SET lock_timeout = '2s';

CREATE OR REPLACE FUNCTION ats_jobs_search_azienda()
RETURNS trigger AS $$
DECLARE
    pid TEXT := COALESCE(NEW.platform_id, OLD.platform_id);
    s TEXT := COALESCE(NEW.slug, OLD.slug);
BEGIN
    INSERT INTO ats_jobs_search_outbox (job_id, op)
    SELECT id, 'upsert' FROM ats_jobs
     WHERE platform_id = pid AND slug = s AND expired_at IS NULL
    ON CONFLICT (job_id) DO UPDATE SET op='upsert', queued_at=now();
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

ALTER TABLE ats_companies
  ADD COLUMN IF NOT EXISTS source_type VARCHAR(32) NOT NULL DEFAULT 'direct';
ALTER TABLE ats_companies
  ADD CONSTRAINT ats_companies_source_type_check
  CHECK (source_type IN ('direct', 'aggregator', 'staffing', 'public_board')) NOT VALID;
ALTER TABLE ats_companies VALIDATE CONSTRAINT ats_companies_source_type_check;
CREATE INDEX IF NOT EXISTS ats_companies_source_type_platform_idx
  ON ats_companies (source_type, platform_id);
