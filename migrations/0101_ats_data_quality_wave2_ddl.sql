-- nivult:require-table public.ats_jobs
SET LOCAL lock_timeout = '4s';

ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS source_type TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS content_updated_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS absent_since TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS absence_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS active_since TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS closure_reason TEXT;

CREATE OR REPLACE FUNCTION riempi_posted_at() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND NEW.posted_at IS NULL THEN
    NEW.posted_at := OLD.posted_at;
    NEW.posted_at_estimated := OLD.posted_at_estimated;
    RETURN NEW;
  END IF;
  IF NEW.posted_at IS NOT NULL
     AND (NEW.posted_at < TIMESTAMPTZ '2020-01-01 00:00:00+00'
          OR NEW.posted_at > now() + interval '2 days') THEN
    IF TG_OP = 'UPDATE' AND OLD.posted_at IS NOT NULL
       AND OLD.posted_at >= TIMESTAMPTZ '2020-01-01 00:00:00+00'
       AND OLD.posted_at <= now() + interval '2 days' THEN
      NEW.posted_at := OLD.posted_at;
      NEW.posted_at_estimated := OLD.posted_at_estimated;
    ELSE
      NEW.posted_at := greatest(TIMESTAMPTZ '2020-01-01 00:00:00+00',
          least(now(), COALESCE(NEW.created_at, NEW.fetched_at, now())));
      NEW.posted_at_estimated := true;
    END IF;
  ELSIF TG_OP = 'UPDATE' AND OLD.posted_at_estimated
        AND NEW.posted_at IS DISTINCT FROM OLD.posted_at THEN
    NEW.posted_at_estimated := false;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_riempi_posted_at ON ats_jobs;
CREATE TRIGGER trg_riempi_posted_at
  BEFORE UPDATE OF posted_at ON ats_jobs
  FOR EACH ROW EXECUTE FUNCTION riempi_posted_at();
DROP TRIGGER IF EXISTS trg_sanifica_posted_at_insert ON ats_jobs;
CREATE TRIGGER trg_sanifica_posted_at_insert
  BEFORE INSERT ON ats_jobs
  FOR EACH ROW EXECUTE FUNCTION riempi_posted_at();

CREATE OR REPLACE FUNCTION ats_ciclo_vita_guardia() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.active_since := COALESCE(NEW.active_since, NEW.created_at, NEW.fetched_at, now());
    NEW.absent_since := NULL;
    NEW.absence_count := 0;
    RETURN NEW;
  END IF;
  IF NEW.expired_at IS NULL THEN
    IF OLD.expired_at IS NOT NULL THEN
      NEW.active_since := now();
      NEW.closure_reason := NULL;
    ELSE
      NEW.active_since := COALESCE(OLD.active_since, OLD.created_at, OLD.fetched_at, now());
    END IF;
    NEW.absent_since := NULL;
    NEW.absence_count := 0;
  ELSIF OLD.expired_at IS NULL THEN
    NEW.closure_reason := COALESCE(NEW.closure_reason, 'sconosciuta');
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_ats_ciclo_vita_guardia ON ats_jobs;
CREATE TRIGGER trg_ats_ciclo_vita_guardia
  BEFORE INSERT OR UPDATE OF expired_at, fetched_at ON ats_jobs
  FOR EACH ROW EXECUTE FUNCTION ats_ciclo_vita_guardia();

CREATE OR REPLACE FUNCTION ats_content_updated_job() RETURNS trigger AS $f$
BEGIN
    IF COALESCE(current_setting('nivult.skip_content_updated', true), '') = 'on' THEN
        RETURN NEW;
    END IF;
    IF TG_OP = 'INSERT' THEN
        NEW.content_updated_at := COALESCE(NEW.content_updated_at, now());
        RETURN NEW;
    END IF;
    IF ROW(NEW.platform_id, NEW.slug, NEW.external_id, NEW.title, NEW.url,
           NEW.location, NEW.country, NEW.city, NEW.posted_at, NEW.department,
           NEW.raw, NEW.expired_at, NEW.salary_min, NEW.salary_max,
           NEW.salary_currency, NEW.employment_type, NEW.duplicate_key,
           NEW.salary_period, NEW.seniority, NEW.remote, NEW.skills, NEW.lang,
           NEW.contact_email, NEW.languages_required, NEW.orario, NEW.durata,
           NEW.salary_da, NEW.posted_at_estimated, NEW.created_at)
       IS DISTINCT FROM
       ROW(OLD.platform_id, OLD.slug, OLD.external_id, OLD.title, OLD.url,
           OLD.location, OLD.country, OLD.city, OLD.posted_at, OLD.department,
           OLD.raw, OLD.expired_at, OLD.salary_min, OLD.salary_max,
           OLD.salary_currency, OLD.employment_type, OLD.duplicate_key,
           OLD.salary_period, OLD.seniority, OLD.remote, OLD.skills, OLD.lang,
           OLD.contact_email, OLD.languages_required, OLD.orario, OLD.durata,
           OLD.salary_da, OLD.posted_at_estimated, OLD.created_at) THEN
        NEW.content_updated_at := now();
    END IF;
    RETURN NEW;
END;
$f$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_zz_content_updated ON ats_jobs;
CREATE TRIGGER trg_zz_content_updated
    BEFORE INSERT OR UPDATE OF platform_id, slug, external_id, title, url,
        location, country, city, posted_at, department, raw, expired_at,
        salary_min, salary_max, salary_currency, employment_type, duplicate_key,
        salary_period, seniority, remote, skills, lang, contact_email,
        languages_required, orario, durata, salary_da, posted_at_estimated, created_at
    ON ats_jobs FOR EACH ROW EXECUTE FUNCTION ats_content_updated_job();

CREATE TABLE IF NOT EXISTS ats_delta_hash (
    job_id UUID PRIMARY KEY REFERENCES ats_jobs(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_emitted_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS ats_delta_state (
    chiave TEXT PRIMARY KEY,
    valore TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ats_jobs_posted_at_minima_check') THEN
    ALTER TABLE ats_jobs ADD CONSTRAINT ats_jobs_posted_at_minima_check
      CHECK (posted_at IS NULL OR posted_at >= TIMESTAMPTZ '2020-01-01 00:00:00+00') NOT VALID;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ats_jobs_absence_count_check') THEN
    ALTER TABLE ats_jobs ADD CONSTRAINT ats_jobs_absence_count_check
      CHECK (absence_count >= 0) NOT VALID;
  END IF;
END $$;

GRANT SELECT ON ats_delta_hash, ats_delta_state TO nivult_app;
