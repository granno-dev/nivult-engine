CREATE INDEX CONCURRENTLY IF NOT EXISTS tecnologie_ricerca_ordine_idx
    ON tecnologie_ricerca (tecnologia, posted_at DESC NULLS LAST, job_id);

CREATE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_attive_platform_country_v2_idx
    ON ats_jobs (platform_id, country, posted_at DESC NULLS LAST, id)
    WHERE expired_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_attive_country_v2_idx
    ON ats_jobs (country, posted_at DESC NULLS LAST, id)
    WHERE expired_at IS NULL;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_attive_platform_country_senior_remote_idx
    ON ats_jobs (platform_id, country, seniority, remote, posted_at DESC NULLS LAST, id)
    WHERE expired_at IS NULL;
