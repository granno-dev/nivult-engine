-- 0004 — Offerte, embedding, appartenenza ai cluster.

CREATE TABLE jobs (
  id                    uuid        PRIMARY KEY DEFAULT gen_random_uuid(),

  source                text        NOT NULL CHECK (source IN (
                                      'fantastic','france_travail','bundesagentur',
                                      'arbetsformedlingen','nav','tyomarkkinatori')),
  source_job_id         text        NOT NULL,

  url                   text        NOT NULL,
  -- url normalizzato: schema+host minuscoli, niente query string, niente UTM,
  -- niente fragment, niente slash finale. Lo calcola l'adapter di ingestione.
  canonical_url         text        NOT NULL,
  domain_derived        text,
  org_linkedin_slug     text,

  title                 text        NOT NULL,
  title_normalized      text        NOT NULL,
  organization          text        NOT NULL,
  date_posted           timestamptz NOT NULL,

  cities                text[]      NOT NULL DEFAULT '{}',
  countries             text[]      NOT NULL DEFAULT '{}',
  locations             jsonb,

  -- Campi AI dichiarati al 100% dal fornitore.
  -- ai_visa_sponsorship è nullable nonostante sia "garantito": l'adapter
  -- normalizza a booleano e mappa a NULL i valori non interpretabili, invece di
  -- far fallire l'ingestione. Il valore originale resta comunque in raw.
  ai_job_language          text,
  ai_visa_sponsorship      boolean,
  ai_work_arrangement      text,
  ai_experience_level      text,
  ai_employment_type       text,
  ai_working_hours         text,
  ai_key_skills            text[]   NOT NULL DEFAULT '{}',
  ai_keywords              text[]   NOT NULL DEFAULT '{}',
  ai_taxonomies_a          text[]   NOT NULL DEFAULT '{}',
  ai_requirements_summary  text,
  ai_core_responsibilities text,

  -- Campi parziali. Tutti nullable, e nessuna logica del motore può
  -- presupporne la presenza.
  salary                          jsonb,
  date_valid_through              timestamptz,
  ai_education                    text,
  organization_logo               text,
  ai_work_arrangement_office_days smallint CHECK (ai_work_arrangement_office_days BETWEEN 0 AND 7),

  -- Payload integro della fonte. Esiste perché un campo nuovo del fornitore non
  -- richieda una migrazione per essere recuperato a posteriori.
  raw                   jsonb       NOT NULL,

  tsv                   tsvector,
  -- Predisposta per il passaggio alla configurazione full-text per lingua.
  -- Oggi il trigger usa sempre 'simple'.
  text_search_config    regconfig   NOT NULL DEFAULT 'simple',

  -- Impronta per la deduplica morbida (quasi-duplicati con URL diversi).
  fingerprint           text,

  status                text        NOT NULL DEFAULT 'active'
                                    CHECK (status IN ('active','expired','removed')),
  first_seen_at         timestamptz NOT NULL DEFAULT now(),
  last_seen_alive_at    timestamptz NOT NULL DEFAULT now(),
  expired_at            timestamptz,

  duplicate_of_job_id   uuid        REFERENCES jobs(id) ON DELETE SET NULL,

  CONSTRAINT jobs_source_id_key      UNIQUE (source, source_job_id),
  -- Deduplica dura. Regge perché per policy accettiamo solo link a career site
  -- aziendali: stessa pagina del career site = stessa offerta.
  CONSTRAINT jobs_canonical_url_key  UNIQUE (canonical_url),
  CONSTRAINT jobs_expiry_ck          CHECK ((status = 'active') = (expired_at IS NULL)),
  CONSTRAINT jobs_not_self_dup_ck    CHECK (duplicate_of_job_id IS NULL
                                            OR duplicate_of_job_id <> id)
);

-- Tutti gli indici di ricerca sono parziali sull'insieme che il funnel guarda
-- davvero: offerte vive e canoniche.
CREATE INDEX jobs_active_posted_idx ON jobs (date_posted DESC)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;

CREATE INDEX jobs_taxonomies_idx ON jobs USING gin (ai_taxonomies_a)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;
CREATE INDEX jobs_countries_idx  ON jobs USING gin (countries)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;
CREATE INDEX jobs_key_skills_idx ON jobs USING gin (ai_key_skills)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;
CREATE INDEX jobs_keywords_idx   ON jobs USING gin (ai_keywords)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;
CREATE INDEX jobs_tsv_idx        ON jobs USING gin (tsv)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;

-- Filtri deterministici dell'imbuto.
CREATE INDEX jobs_filters_idx ON jobs (ai_experience_level, ai_work_arrangement, ai_job_language)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;

-- Sweep di scadenza: offerte non più viste vive.
CREATE INDEX jobs_last_seen_idx ON jobs (last_seen_alive_at) WHERE status = 'active';

CREATE INDEX jobs_fingerprint_idx ON jobs (fingerprint) WHERE duplicate_of_job_id IS NULL;
CREATE INDEX jobs_duplicate_of_idx ON jobs (duplicate_of_job_id) WHERE duplicate_of_job_id IS NOT NULL;


-- tsv e fingerprint sono calcolati da trigger e non da colonne generate.
-- Le colonne generate richiedono espressioni IMMUTABLE, e le funzioni che
-- servirebbero qui (array_to_string, conversioni di fuso su timestamptz) sono
-- STABLE. Il trigger non ha quel vincolo, ed è anche il punto giusto dove
-- innestare la configurazione full-text per lingua quando ci arriveremo.
CREATE OR REPLACE FUNCTION jobs_derive_fields() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_skills text;
BEGIN
  v_skills := COALESCE(array_to_string(NEW.ai_key_skills, ' '), '');

  NEW.tsv :=
      setweight(to_tsvector('simple', COALESCE(NEW.title, '')), 'A')
   || setweight(to_tsvector('simple', COALESCE(NEW.organization, '')), 'B')
   || setweight(to_tsvector('simple', v_skills), 'B')
   || setweight(to_tsvector('simple',
        COALESCE(NEW.ai_requirements_summary, '') || ' ' ||
        COALESCE(NEW.ai_core_responsibilities, '')), 'C');

  -- Quasi-duplicati: stessa azienda, stesso titolo normalizzato, stesso paese,
  -- stessa settimana di pubblicazione. Non blocca l'inserimento — serve al job
  -- di deduplica per popolare duplicate_of_job_id.
  NEW.fingerprint := encode(sha256(convert_to(
      COALESCE(NEW.domain_derived, '') || '|' ||
      NEW.title_normalized            || '|' ||
      COALESCE(NEW.countries[1], '')  || '|' ||
      to_char(NEW.date_posted AT TIME ZONE 'UTC', 'IYYY-IW'),
    'UTF8')), 'hex');

  RETURN NEW;
END $$;

CREATE TRIGGER jobs_derive_fields_trg
  BEFORE INSERT OR UPDATE ON jobs
  FOR EACH ROW EXECUTE FUNCTION jobs_derive_fields();


-- Vieta le catene di duplicati (A -> B -> C): il bersaglio di un duplicato deve
-- essere esso stesso canonico. Senza questo, risolvere l'originale richiede una
-- ricorsione, e la ricorsione prima o poi trova un ciclo.
CREATE OR REPLACE FUNCTION assert_duplicate_target_is_canonical() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_target_dup uuid;
BEGIN
  IF NEW.duplicate_of_job_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT duplicate_of_job_id INTO v_target_dup FROM jobs WHERE id = NEW.duplicate_of_job_id;
  IF v_target_dup IS NOT NULL THEN
    RAISE EXCEPTION 'catena di duplicati: % punta a %, che è già un duplicato di %',
      NEW.id, NEW.duplicate_of_job_id, v_target_dup
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER jobs_duplicate_target_canonical
  BEFORE INSERT OR UPDATE OF duplicate_of_job_id ON jobs
  FOR EACH ROW EXECUTE FUNCTION assert_duplicate_target_is_canonical();


-- Embedding in tabella separata.
--
-- Un vector(1024) sono ~4 KB per riga, sopra la soglia di TOAST. Dentro jobs
-- appesantirebbe ogni scansione che l'embedding non usa, e la compressione TOAST
-- peggiora la ricerca vettoriale. Separandolo, jobs resta stretta per i filtri
-- deterministici e questa tabella resta densa e adatta all'indice HNSW.
-- Vantaggio secondario: cambiare modello di embedding significa ricostruire
-- solo questa.
CREATE TABLE job_embeddings (
  job_id       uuid        PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
  embedding    vector(1024) NOT NULL,
  model        text        NOT NULL,
  generated_at timestamptz NOT NULL DEFAULT now()
);

-- Indice non parziale: non può filtrare su jobs.status, che sta in un'altra
-- tabella. È esattamente il caso per cui in 0001 abbiamo attivato
-- hnsw.iterative_scan.
CREATE INDEX job_embeddings_hnsw_idx ON job_embeddings
  USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);


CREATE TABLE job_clusters (
  job_id           uuid        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  cluster_id       uuid        NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  first_seen_at    timestamptz NOT NULL DEFAULT now(),
  ingestion_run_id uuid,  -- FK aggiunta in 0006, dove nasce ingestion_runs
  PRIMARY KEY (job_id, cluster_id)
);

-- Il worker itera sui cluster: "dammi le offerte nuove di questo cluster".
CREATE INDEX job_clusters_by_cluster_idx ON job_clusters (cluster_id, first_seen_at DESC);
