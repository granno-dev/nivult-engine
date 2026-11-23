CREATE TABLE IF NOT EXISTS ats_jobs_search_outbox (
    job_id UUID PRIMARY KEY,
    op TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ats_jobs_search_outbox_queued_idx
    ON ats_jobs_search_outbox (queued_at);

CREATE OR REPLACE FUNCTION ats_jobs_search_raw(raw JSONB) RETURNS JSONB
LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_build_object(
        'description', raw->'description', 'content', raw->'content',
        'descriptionHtml', raw->'descriptionHtml', 'descriptionPlain', raw->'descriptionPlain',
        'externalDescription', raw->'externalDescription', 'jobDescription', raw->'jobDescription',
        'job_description', raw->'job_description', 'Job_Description', raw->'Job_Description',
        'body', raw->'body', 'content_html', raw->'content_html',
        'description_html', raw->'description_html', 'descriptionBody', raw->'descriptionBody',
        'text', raw->'text', '_jobposting', raw->'_jobposting',
        'ShortDescriptionStr', raw->'ShortDescriptionStr', 'company', raw->'company')
$$;

CREATE OR REPLACE FUNCTION ats_jobs_search_enqueue_job(job UUID, operation TEXT)
RETURNS void LANGUAGE sql AS $$
    INSERT INTO ats_jobs_search_outbox (job_id, op) VALUES (job, operation)
    ON CONFLICT (job_id) DO UPDATE SET op=EXCLUDED.op, queued_at=now()
$$;

CREATE OR REPLACE FUNCTION ats_jobs_search_cambia() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    job UUID;
    operazione TEXT;
    vecchia_chiave TEXT := NULL;
    nuova_chiave TEXT := NULL;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF NOT (ROW(OLD.platform_id, OLD.slug, OLD.external_id, OLD.title, OLD.url,
                    OLD.location, OLD.country, OLD.city, OLD.posted_at, OLD.department,
                    OLD.expired_at, OLD.salary_min, OLD.salary_max, OLD.salary_currency,
                    OLD.salary_period, OLD.seniority, OLD.remote, OLD.skills, OLD.lang,
                    OLD.languages_required, OLD.employment_type, OLD.orario, OLD.durata,
                    OLD.salary_da, OLD.posted_at_estimated, OLD.duplicate_key, OLD.created_at)
                IS DISTINCT FROM
                ROW(NEW.platform_id, NEW.slug, NEW.external_id, NEW.title, NEW.url,
                    NEW.location, NEW.country, NEW.city, NEW.posted_at, NEW.department,
                    NEW.expired_at, NEW.salary_min, NEW.salary_max, NEW.salary_currency,
                    NEW.salary_period, NEW.seniority, NEW.remote, NEW.skills, NEW.lang,
                    NEW.languages_required, NEW.employment_type, NEW.orario, NEW.durata,
                    NEW.salary_da, NEW.posted_at_estimated, NEW.duplicate_key, NEW.created_at)
                OR ats_jobs_search_raw(OLD.raw) IS DISTINCT FROM ats_jobs_search_raw(NEW.raw)) THEN
            RETURN NEW;
        END IF;
        vecchia_chiave := OLD.duplicate_key;
        nuova_chiave := NEW.duplicate_key;
        job := NEW.id;
        operazione := CASE WHEN NEW.expired_at IS NULL THEN 'upsert' ELSE 'delete' END;
    ELSIF TG_OP = 'INSERT' THEN
        job := NEW.id;
        nuova_chiave := NEW.duplicate_key;
        operazione := CASE WHEN NEW.expired_at IS NULL THEN 'upsert' ELSE 'delete' END;
    ELSE
        job := OLD.id;
        vecchia_chiave := OLD.duplicate_key;
        operazione := 'delete';
    END IF;
    PERFORM ats_jobs_search_enqueue_job(job, operazione);
    WITH chiavi AS (
        SELECT DISTINCT k FROM unnest(ARRAY[vecchia_chiave, nuova_chiave]) AS k
        WHERE k IS NOT NULL
    )
    INSERT INTO ats_jobs_search_outbox (job_id, op)
    SELECT j.id, 'upsert' FROM ats_jobs j JOIN chiavi c ON j.duplicate_key=c.k
    ON CONFLICT (job_id) DO UPDATE SET op='upsert', queued_at=now();
    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_opensearch_jobs ON ats_jobs;
CREATE TRIGGER trg_opensearch_jobs
AFTER INSERT OR UPDATE OR DELETE ON ats_jobs
FOR EACH ROW EXECUTE FUNCTION ats_jobs_search_cambia();

CREATE OR REPLACE FUNCTION ats_jobs_search_collegato() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE job UUID := COALESCE(NEW.job_id, OLD.job_id);
BEGIN
    PERFORM ats_jobs_search_enqueue_job(job, 'upsert');
    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_opensearch_classificazione ON job_classifications;
CREATE TRIGGER trg_opensearch_classificazione
AFTER INSERT OR DELETE OR UPDATE OF family ON job_classifications
FOR EACH ROW EXECUTE FUNCTION ats_jobs_search_collegato();
DROP TRIGGER IF EXISTS trg_opensearch_tecnologie ON tecnologie_v1;
CREATE TRIGGER trg_opensearch_tecnologie
AFTER INSERT OR DELETE OR UPDATE OF tecnologie ON tecnologie_v1
FOR EACH ROW EXECUTE FUNCTION ats_jobs_search_collegato();
DROP TRIGGER IF EXISTS trg_opensearch_estrazioni ON estrazioni_v2b;
CREATE TRIGGER trg_opensearch_estrazioni
AFTER INSERT OR DELETE OR UPDATE OF sintesi ON estrazioni_v2b
FOR EACH ROW EXECUTE FUNCTION ats_jobs_search_collegato();
DROP TRIGGER IF EXISTS trg_opensearch_mt5 ON sintesi_mt5;
CREATE TRIGGER trg_opensearch_mt5
AFTER INSERT OR DELETE OR UPDATE OF sintesi_pulita, ripassata_at ON sintesi_mt5
FOR EACH ROW EXECUTE FUNCTION ats_jobs_search_collegato();
DROP TRIGGER IF EXISTS trg_opensearch_dettagli ON offerte_dettagli;
CREATE TRIGGER trg_opensearch_dettagli
AFTER INSERT OR DELETE OR UPDATE OF management_level, is_decision_maker,
    shift_schedule, work_hours, is_urgently_hiring, benefits, salary_text,
    valid_through, state, postal_code, latitude, longitude, geo_da,
    applicants_count, is_easy_apply
ON offerte_dettagli FOR EACH ROW EXECUTE FUNCTION ats_jobs_search_collegato();

CREATE OR REPLACE FUNCTION ats_jobs_search_azienda() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    pid TEXT := COALESCE(NEW.platform_id, OLD.platform_id);
    s TEXT := COALESCE(NEW.slug, OLD.slug);
BEGIN
    INSERT INTO ats_jobs_search_outbox (job_id, op)
    SELECT id, 'upsert' FROM ats_jobs WHERE platform_id=pid AND slug=s
    ON CONFLICT (job_id) DO UPDATE SET op='upsert', queued_at=now();
    RETURN COALESCE(NEW, OLD);
END;
$$;
DROP TRIGGER IF EXISTS trg_opensearch_azienda ON ats_companies;
CREATE TRIGGER trg_opensearch_azienda
AFTER INSERT OR DELETE OR UPDATE OF company_name ON ats_companies
FOR EACH ROW EXECUTE FUNCTION ats_jobs_search_azienda();

DROP TRIGGER IF EXISTS trg_opensearch_benchmark ON stipendi_benchmark;
DROP FUNCTION IF EXISTS ats_jobs_search_benchmark();
