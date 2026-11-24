SET lock_timeout = '2s';

CREATE OR REPLACE FUNCTION ats_jobs_search_azienda()
RETURNS trigger AS $$
DECLARE
    pid TEXT := COALESCE(NEW.platform_id, OLD.platform_id);
    s TEXT := COALESCE(NEW.slug, OLD.slug);
BEGIN
    IF TG_OP = 'INSERT' AND NEW.company_name IS NULL THEN
        RETURN NEW;
    END IF;
    INSERT INTO ats_jobs_search_outbox (job_id, op)
    SELECT id, 'upsert' FROM ats_jobs
     WHERE platform_id = pid AND slug = s AND expired_at IS NULL
    ON CONFLICT (job_id) DO UPDATE SET op='upsert', queued_at=now();
    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;
