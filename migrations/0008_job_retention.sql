-- 0008 — Retention delle offerte morte e contatori aggregati.
--
-- Regola: status 'expired' o 'removed' da più di 60 giorni sparisce, jsonb
-- grezzo compreso. Le offerte 'active' non scadono mai.
--
-- Ma un'offerta già valutata o già inviata non può semplicemente sparire:
--   - matches.job_id e digest_items.job_id sono ON DELETE RESTRICT;
--   - la UNIQUE (user_id, job_id) su matches è l'anti-ripetizione, e cancellare
--     il match significa che la stessa offerta può tornare all'utente;
--   - digest_items è il registro di cosa un utente ha davvero ricevuto.
--
-- Quindi due livelli:
--   1. offerta morta e NON referenziata  -> DELETE vero e proprio;
--   2. offerta morta e referenziata      -> svuotata (raw, embedding, tsv, testi
--      lunghi) e marcata con purged_at, lasciando una riga-lapide di poche
--      centinaia di byte che tiene in piedi lo storico e l'anti-ripetizione.
-- In entrambi i casi il jsonb grezzo, che è il grosso del volume, sparisce.

ALTER TABLE jobs ADD COLUMN purged_at timestamptz;

-- raw diventa nullable solo per le lapidi: un'offerta non svuotata deve sempre
-- avere il suo payload di origine.
ALTER TABLE jobs ALTER COLUMN raw DROP NOT NULL;
ALTER TABLE jobs ADD CONSTRAINT jobs_raw_present_ck
  CHECK (purged_at IS NOT NULL OR raw IS NOT NULL);

-- Un'offerta svuotata è per definizione morta.
ALTER TABLE jobs ADD CONSTRAINT jobs_purged_is_dead_ck
  CHECK (purged_at IS NULL OR status <> 'active');

-- La coda della retention. purged_at IS NULL nel predicato è anche la garanzia
-- di idempotenza: una lapide non viene ripresa al giro successivo.
CREATE INDEX jobs_purgeable_idx ON jobs (expired_at)
  WHERE status IN ('expired','removed') AND purged_at IS NULL;


-- Contatori aggregati per cluster e per mese.
-- Nessun riferimento a utenti, per costruzione: quando le offerte spariscono le
-- statistiche restano, ma non è ricostruibile chi ha visto cosa.
CREATE TABLE cluster_month_stats (
  cluster_id        uuid    NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  -- primo giorno del mese di date_posted
  month             date    NOT NULL,

  jobs_purged       integer NOT NULL DEFAULT 0 CHECK (jobs_purged >= 0),
  jobs_tombstoned   integer NOT NULL DEFAULT 0 CHECK (jobs_tombstoned >= 0),
  expired_count     integer NOT NULL DEFAULT 0 CHECK (expired_count >= 0),
  removed_count     integer NOT NULL DEFAULT 0 CHECK (removed_count >= 0),
  -- Copertura del campo salary, che il fornitore riempie in meno del 30% dei
  -- casi: qui si vede se sta migliorando o peggiorando nel tempo.
  with_salary_count integer NOT NULL DEFAULT 0 CHECK (with_salary_count >= 0),
  -- Somma e conteggio invece della media: così i lotti successivi si sommano.
  lifetime_days_sum bigint  NOT NULL DEFAULT 0 CHECK (lifetime_days_sum >= 0),
  lifetime_count    integer NOT NULL DEFAULT 0 CHECK (lifetime_count >= 0),

  last_updated_at   timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (cluster_id, month),
  CONSTRAINT cluster_month_stats_month_ck
    CHECK (month = date_trunc('month', month)::date)
);

COMMENT ON TABLE cluster_month_stats IS
  'Aggregati che sopravvivono alla cancellazione del corpus. Nessun dato per utente.';

CREATE VIEW cluster_month_stats_v AS
SELECT s.*,
       CASE WHEN s.lifetime_count > 0
            THEN round(s.lifetime_days_sum::numeric / s.lifetime_count, 1) END
         AS avg_lifetime_days,
       c.family, c.country
FROM cluster_month_stats s
JOIN clusters c ON c.id = s.cluster_id;


-- Il trigger di derivazione non deve ricalcolare tsv su una lapide: senza
-- questa uscita anticipata l'UPDATE che svuota la riga se lo ripopolerebbe da
-- title e organization.
CREATE OR REPLACE FUNCTION jobs_derive_fields() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_skills text;
BEGIN
  IF NEW.purged_at IS NOT NULL THEN
    NEW.tsv := NULL;
    RETURN NEW;
  END IF;

  v_skills := COALESCE(array_to_string(NEW.ai_key_skills, ' '), '');

  NEW.tsv :=
      setweight(to_tsvector('simple', COALESCE(NEW.title, '')), 'A')
   || setweight(to_tsvector('simple', COALESCE(NEW.organization, '')), 'B')
   || setweight(to_tsvector('simple', v_skills), 'B')
   || setweight(to_tsvector('simple',
        COALESCE(NEW.ai_requirements_summary, '') || ' ' ||
        COALESCE(NEW.ai_core_responsibilities, '')), 'C');

  NEW.fingerprint := encode(sha256(convert_to(
      COALESCE(NEW.domain_derived, '') || '|' ||
      NEW.title_normalized            || '|' ||
      COALESCE(NEW.countries[1], '')  || '|' ||
      to_char(NEW.date_posted AT TIME ZONE 'UTC', 'IYYY-IW'),
    'UTF8')), 'hex');

  RETURN NEW;
END $$;


-- Un lotto di retention. Il chiamante richiama finché non ritorna (0, 0).
-- Ogni lotto è una transazione breve: la retention non deve tenere lock lunghi
-- su jobs, che è la tabella più calda del motore.
CREATE OR REPLACE FUNCTION purge_dead_jobs(
  p_older_than_days integer DEFAULT 60,
  p_batch_size      integer DEFAULT 1000)
RETURNS TABLE (jobs_deleted bigint, jobs_tombstoned bigint)
LANGUAGE plpgsql AS $$
DECLARE
  v_ids  uuid[];
  v_del  uuid[];
  v_tomb uuid[];
BEGIN
  IF p_older_than_days < 0 OR p_batch_size <= 0 THEN
    RAISE EXCEPTION 'parametri non validi: giorni=%, lotto=%',
      p_older_than_days, p_batch_size;
  END IF;

  SELECT array_agg(id) INTO v_ids FROM (
    SELECT id FROM jobs
     WHERE status IN ('expired','removed')
       AND purged_at IS NULL
       AND expired_at < now() - make_interval(days => p_older_than_days)
     ORDER BY expired_at
     LIMIT p_batch_size
     FOR UPDATE SKIP LOCKED
  ) s;

  IF v_ids IS NULL THEN
    jobs_deleted := 0; jobs_tombstoned := 0;
    RETURN NEXT; RETURN;
  END IF;

  -- Referenziata da uno storico che va conservato?
  SELECT COALESCE(array_agg(id) FILTER (WHERE NOT ref), '{}'),
         COALESCE(array_agg(id) FILTER (WHERE ref),     '{}')
    INTO v_del, v_tomb
  FROM (
    SELECT j.id,
           EXISTS (SELECT 1 FROM matches      m  WHERE m.job_id  = j.id)
        OR EXISTS (SELECT 1 FROM digest_items di WHERE di.job_id = j.id) AS ref
      FROM jobs j WHERE j.id = ANY(v_ids)
  ) t;

  -- 1. Aggregare PRIMA di toccare qualunque cosa: dopo il DELETE le righe di
  --    job_clusters sono già andate in cascata e l'attribuzione al cluster
  --    sarebbe persa.
  INSERT INTO cluster_month_stats AS s (
    cluster_id, month, jobs_purged, jobs_tombstoned, expired_count,
    removed_count, with_salary_count, lifetime_days_sum, lifetime_count)
  SELECT jc.cluster_id,
         date_trunc('month', j.date_posted AT TIME ZONE 'UTC')::date,
         count(*) FILTER (WHERE j.id = ANY(v_del)),
         count(*) FILTER (WHERE j.id = ANY(v_tomb)),
         count(*) FILTER (WHERE j.status = 'expired'),
         count(*) FILTER (WHERE j.status = 'removed'),
         count(*) FILTER (WHERE j.salary IS NOT NULL),
         COALESCE(sum(GREATEST(0, floor(
           EXTRACT(epoch FROM (j.expired_at - j.date_posted)) / 86400)))::bigint, 0),
         count(*) FILTER (WHERE j.expired_at IS NOT NULL)
    FROM jobs j
    JOIN job_clusters jc ON jc.job_id = j.id
   WHERE j.id = ANY(v_ids)
   GROUP BY 1, 2
  ON CONFLICT (cluster_id, month) DO UPDATE SET
    jobs_purged       = s.jobs_purged       + EXCLUDED.jobs_purged,
    jobs_tombstoned   = s.jobs_tombstoned   + EXCLUDED.jobs_tombstoned,
    expired_count     = s.expired_count     + EXCLUDED.expired_count,
    removed_count     = s.removed_count     + EXCLUDED.removed_count,
    with_salary_count = s.with_salary_count + EXCLUDED.with_salary_count,
    lifetime_days_sum = s.lifetime_days_sum + EXCLUDED.lifetime_days_sum,
    lifetime_count    = s.lifetime_count    + EXCLUDED.lifetime_count,
    last_updated_at   = now();

  -- 2. Lapidi: via il peso, resta l'identità.
  --
  -- ATTENZIONE, questa lista è portante: title, organization, url e date_posted
  -- NON vanno azzerati. Sono ciò che un utente vede riaprendo un digest di mesi
  -- fa; digest_items conserva punteggio e motivazione, ma di quale offerta si
  -- parlasse lo dice solo questa riga. Aggiungerli qui renderebbe illeggibile
  -- tutto lo storico delle consegne, in silenzio.
  -- Bloccato da un test in scripts/check_modules.py.
  DELETE FROM job_embeddings WHERE job_id = ANY(v_tomb);
  UPDATE jobs SET
      raw                     = NULL,
      locations               = NULL,
      salary                  = NULL,
      ai_requirements_summary = NULL,
      ai_core_responsibilities= NULL,
      ai_key_skills           = '{}',
      ai_keywords             = '{}',
      ai_taxonomies_a         = '{}',
      organization_logo       = NULL,
      purged_at               = now()
    WHERE id = ANY(v_tomb);

  -- 3. Cancellazione vera: job_clusters e job_embeddings vanno in cascata.
  DELETE FROM jobs WHERE id = ANY(v_del);

  jobs_deleted    := COALESCE(array_length(v_del, 1), 0);
  jobs_tombstoned := COALESCE(array_length(v_tomb, 1), 0);
  RETURN NEXT;
END $$;

COMMENT ON FUNCTION purge_dead_jobs(integer, integer) IS
  'Un lotto di retention. Richiamare finché non ritorna (0,0). Vedi nivult.retention.';
