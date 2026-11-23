-- La testa tecnologie di v1 scrive qui, non in estrazioni_v2b.
--
-- estrazioni_v2b ha job_id come chiave primaria: una riga per annuncio. Scrivere
-- li' cancellerebbe l'uscita del 2B, e per settimane vorremo confrontare i due
-- sullo stesso annuncio — e poter tornare indietro se la testa sbaglia su
-- qualcosa che il golden non copriva.
--
-- Le tecnologie sono stringhe PRESE DAL TESTO (la testa marca, non riscrive):
-- «excel» se l'annuncio scrive «excel». La normalizzazione in forma commerciale
-- sta a valle, nella tabella degli alias, e si puo' cambiare senza rifare
-- l'estrazione.

CREATE TABLE IF NOT EXISTS tecnologie_v1 (
  job_id      uuid        PRIMARY KEY,
  tecnologie  jsonb       NOT NULL,          -- ["excel", "sap", ...] come nel testo
  quante      int         NOT NULL,          -- ridondante ma comodo per le viste
  modello     text        NOT NULL,          -- es. 'tec-v1-ck02500'
  soglia      real        NOT NULL,          -- la manopola usata: serve a rifare il conto
  testo_hash  text,                          -- per le gemelle: stesso testo, stessa risposta
  creato_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tecnologie_v1_creato ON tecnologie_v1 (creato_at);
CREATE INDEX IF NOT EXISTS tecnologie_v1_hash   ON tecnologie_v1 (testo_hash);
-- per cercare le aziende che usano una tecnologia: e' la query del prodotto
CREATE INDEX IF NOT EXISTS tecnologie_v1_gin    ON tecnologie_v1 USING gin (tecnologie);
CREATE INDEX IF NOT EXISTS tecnologie_v1_tecnologie_ci_gin
  ON tecnologie_v1 USING gin ((lower(tecnologie::text)::jsonb));

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

CREATE INDEX IF NOT EXISTS tecnologie_ricerca_ordine_idx
    ON tecnologie_ricerca (tecnologia, posted_at DESC NULLS LAST, job_id);
CREATE INDEX IF NOT EXISTS ats_jobs_attive_platform_country_v2_idx
    ON ats_jobs (platform_id, country, posted_at DESC NULLS LAST, id)
    WHERE expired_at IS NULL;
CREATE INDEX IF NOT EXISTS ats_jobs_attive_country_v2_idx
    ON ats_jobs (country, posted_at DESC NULLS LAST, id)
    WHERE expired_at IS NULL;
CREATE INDEX IF NOT EXISTS ats_jobs_attive_platform_country_senior_remote_idx
    ON ats_jobs (platform_id, country, seniority, remote, posted_at DESC NULLS LAST, id)
    WHERE expired_at IS NULL;

-- Il marcatore della coda, come locale_v1_at per v1 e estratto_2b_at per il 2B.
-- Due colonne: quando e' stato fatto, e quando e' stato PRENOTATO (la prenotazione
-- scade, cosi' se un operaio muore a meta' le righe tornano libere da sole).
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS tec_v1_at   timestamptz;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS preso_tec_at timestamptz;

-- L'indice parziale che rende istantanea la ricerca della coda. Senza, la query
-- di prenotazione legge l'intera tabella: e' il guasto del 16/09 che teneva il
-- demone del 2B fermo per minuti a ogni giro.
CREATE INDEX IF NOT EXISTS ats_jobs_tec_v1_coda
    ON ats_jobs (posted_at DESC NULLS LAST)
 WHERE expired_at IS NULL AND tec_v1_at IS NULL;
