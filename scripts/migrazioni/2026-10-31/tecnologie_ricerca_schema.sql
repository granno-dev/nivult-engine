CREATE TABLE IF NOT EXISTS tecnologie_ricerca (
    job_id      UUID,
    tecnologia  TEXT,
    posted_at   TIMESTAMPTZ,
    PRIMARY KEY (job_id, tecnologia)
);

CREATE OR REPLACE FUNCTION tecnologie_ricerca_rigenera_job(p_job_id UUID)
RETURNS void LANGUAGE sql AS $f$
    DELETE FROM tecnologie_ricerca WHERE job_id = p_job_id;

    INSERT INTO tecnologie_ricerca (job_id, tecnologia, posted_at)
    SELECT DISTINCT j.id, lower(elemento.valore #>> '{}'), j.posted_at
      FROM tecnologie_v1 tv
      JOIN ats_jobs j ON j.id = tv.job_id
      CROSS JOIN LATERAL jsonb_array_elements(
          CASE WHEN jsonb_typeof(tv.tecnologie) = 'array'
               THEN tv.tecnologie ELSE '[]'::jsonb END
      ) AS elemento(valore)
     WHERE tv.job_id = p_job_id
       AND jsonb_typeof(elemento.valore) = 'string'
    ON CONFLICT (job_id, tecnologia) DO NOTHING;
$f$;

CREATE OR REPLACE FUNCTION tecnologie_ricerca_sincronizza_v1()
RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' THEN
        PERFORM tecnologie_ricerca_rigenera_job(OLD.job_id);
        RETURN NULL;
    END IF;
    IF TG_OP = 'UPDATE' AND NEW.job_id IS DISTINCT FROM OLD.job_id THEN
        PERFORM tecnologie_ricerca_rigenera_job(OLD.job_id);
    END IF;
    PERFORM tecnologie_ricerca_rigenera_job(NEW.job_id);
    RETURN NULL;
END;
$f$;

CREATE OR REPLACE FUNCTION tecnologie_ricerca_aggiorna_posted_at()
RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF NEW.posted_at IS DISTINCT FROM OLD.posted_at THEN
        UPDATE tecnologie_ricerca
           SET posted_at = NEW.posted_at
         WHERE job_id = NEW.id;
    END IF;
    RETURN NULL;
END;
$f$;

DROP TRIGGER IF EXISTS trg_tecnologie_ricerca_v1 ON tecnologie_v1;
CREATE TRIGGER trg_tecnologie_ricerca_v1
    AFTER INSERT OR UPDATE OR DELETE ON tecnologie_v1
    FOR EACH ROW EXECUTE FUNCTION tecnologie_ricerca_sincronizza_v1();

DROP TRIGGER IF EXISTS trg_tecnologie_ricerca_posted_at ON ats_jobs;
CREATE TRIGGER trg_tecnologie_ricerca_posted_at
    AFTER UPDATE OF posted_at ON ats_jobs
    FOR EACH ROW EXECUTE FUNCTION tecnologie_ricerca_aggiorna_posted_at();

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nivult_api_lettura') THEN
        GRANT SELECT ON tecnologie_ricerca TO nivult_api_lettura;
    END IF;
END;
$$;
