-- nivult:no-transaction
-- nivult:require-table public.ats_jobs

-- nivult:drop-invalid-index public.ats_jobs_assenze_idx
CREATE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_assenze_idx
    ON ats_jobs (platform_id, slug, absent_since)
    WHERE expired_at IS NULL AND absent_since IS NOT NULL;

-- nivult:drop-invalid-index public.ats_jobs_zombie_idx
CREATE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_zombie_idx
    ON ats_jobs (active_since, id)
    WHERE expired_at IS NULL AND active_since IS NOT NULL;

-- nivult:drop-invalid-index public.ats_jobs_content_updated_idx
CREATE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_content_updated_idx
    ON ats_jobs (content_updated_at)
    WHERE content_updated_at IS NOT NULL;
