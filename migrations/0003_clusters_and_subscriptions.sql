-- 0003 — Cluster, budget giornaliero con circuit breaker, iscrizioni.

CREATE TABLE clusters (
  id                       uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  family                   text        NOT NULL CHECK (length(btrim(family)) > 0),
  country                  char(2)     NOT NULL CHECK (country ~ '^[A-Z]{2}$'),

  status                   text        NOT NULL DEFAULT 'active'
                                       CHECK (status IN ('active','dormant','paused')),
  daily_credit_cap         integer     NOT NULL CHECK (daily_credit_cap > 0),

  last_fetched_at          timestamptz,
  last_successful_fetch_at timestamptz,
  -- Data della offerta più recente vista: serve a calcolare il time_frame
  -- incrementale della chiamata successiva invece di riscaricare tutto.
  last_seen_posted_at      timestamptz,

  subscriber_count         integer     NOT NULL DEFAULT 0 CHECK (subscriber_count >= 0),

  created_at               timestamptz NOT NULL DEFAULT now(),
  updated_at               timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT clusters_family_country_key UNIQUE (family, country)
);

CREATE INDEX clusters_active_idx ON clusters (last_fetched_at NULLS FIRST)
  WHERE status = 'active';

CREATE TRIGGER clusters_set_updated_at
  BEFORE UPDATE ON clusters
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN clusters.subscriber_count IS
  'Denormalizzato, ricalcolato dal job notturno di riconciliazione. Non autoritativo.';


-- Stato runtime del budget: UNA RIGA PER CLUSTER PER GIORNO.
--
-- Non sono colonne su clusters di proposito. Contatori sulla riga del cluster
-- significherebbero (a) ogni worker di ingestione che prende un lock sulla
-- stessa riga, e (b) un cron di azzeramento a mezzanotte, con l'inevitabile
-- domanda "mezzanotte di quale fuso". Con una riga al giorno il nuovo giorno È
-- una riga nuova: nessun reset, nessuna contesa, e lo storico del budget resta
-- disponibile gratis per l'analisi dei costi.
CREATE TABLE cluster_daily_budget (
  cluster_id        uuid    NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  usage_date        date    NOT NULL DEFAULT current_date,
  credits_used      integer NOT NULL DEFAULT 0 CHECK (credits_used >= 0),
  requests_made     integer NOT NULL DEFAULT 0 CHECK (requests_made >= 0),
  jobs_ingested     integer NOT NULL DEFAULT 0 CHECK (jobs_ingested >= 0),
  circuit_open      boolean NOT NULL DEFAULT false,
  circuit_opened_at timestamptz,
  circuit_reason    text,

  PRIMARY KEY (cluster_id, usage_date),
  CONSTRAINT cluster_daily_budget_circuit_ck
    CHECK (circuit_open = (circuit_opened_at IS NOT NULL))
);

CREATE INDEX cluster_daily_budget_open_idx ON cluster_daily_budget (usage_date)
  WHERE circuit_open;


-- Il circuit breaker, atomico.
--
-- Deve stare in SQL e non nell'applicazione: fra "leggo quanto ho consumato" e
-- "scrivo il nuovo consumo" due worker paralleli sforerebbero il tetto. Qui
-- FOR UPDATE serializza sulla riga del giorno corrente.
--
-- Ritorna true se i crediti sono stati consumati, false se il tetto è stato
-- raggiunto (e in quel caso apre il breaker). Il chiamante NON deve procedere
-- quando riceve false.
CREATE OR REPLACE FUNCTION cluster_try_consume(p_cluster_id uuid, p_credits integer)
RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  v_cap  integer;
  v_used integer;
BEGIN
  IF p_credits < 0 THEN
    RAISE EXCEPTION 'p_credits non può essere negativo: %', p_credits;
  END IF;

  SELECT daily_credit_cap INTO v_cap FROM clusters WHERE id = p_cluster_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'cluster sconosciuto: %', p_cluster_id;
  END IF;

  INSERT INTO cluster_daily_budget (cluster_id, usage_date)
  VALUES (p_cluster_id, current_date)
  ON CONFLICT (cluster_id, usage_date) DO NOTHING;

  SELECT credits_used INTO v_used
  FROM cluster_daily_budget
  WHERE cluster_id = p_cluster_id AND usage_date = current_date
  FOR UPDATE;

  IF v_used + p_credits > v_cap THEN
    UPDATE cluster_daily_budget
       SET circuit_open      = true,
           circuit_opened_at = COALESCE(circuit_opened_at, now()),
           circuit_reason    = COALESCE(circuit_reason,
                                 format('tetto giornaliero raggiunto (%s/%s)', v_used, v_cap))
     WHERE cluster_id = p_cluster_id AND usage_date = current_date;
    RETURN false;
  END IF;

  UPDATE cluster_daily_budget
     SET credits_used  = credits_used + p_credits,
         requests_made = requests_made + 1
   WHERE cluster_id = p_cluster_id AND usage_date = current_date;

  RETURN true;
END $$;

COMMENT ON FUNCTION cluster_try_consume(uuid, integer) IS
  'Circuit breaker atomico. false = tetto raggiunto, non procedere con la chiamata.';


CREATE TABLE user_clusters (
  user_id                uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  -- RESTRICT: un cluster seguito da qualcuno non si cancella per sbaglio.
  cluster_id             uuid        NOT NULL REFERENCES clusters(id) ON DELETE RESTRICT,
  created_at             timestamptz NOT NULL DEFAULT now(),
  is_paused              boolean     NOT NULL DEFAULT false,

  -- Filtri personali, applicati nel primo stadio deterministico dell'imbuto.
  -- Colonne tipizzate e non un jsonb: sono pochi campi stabili che finiscono
  -- dentro un WHERE, e in colonne si possono vincolare e indicizzare.
  min_seniority          text        REFERENCES experience_levels(code),
  max_seniority          text        REFERENCES experience_levels(code),
  work_arrangements      text[]      NOT NULL DEFAULT '{}',
  max_office_days        smallint    CHECK (max_office_days BETWEEN 0 AND 7),
  languages              text[]      NOT NULL DEFAULT '{}',
  employment_types       text[]      NOT NULL DEFAULT '{}',
  needs_visa_sponsorship boolean     NOT NULL DEFAULT false,

  -- NON APPLICABILE con i dati del fornitore attuali: la dimensione azienda non
  -- è fra i campi garantiti né fra quelli parziali di Fantastic.jobs. La colonna
  -- accetta il valore ma il funnel lo ignora finché non abbiamo un
  -- arricchimento a partire da org_linkedin_slug.
  company_sizes          text[]      NOT NULL DEFAULT '{}',

  PRIMARY KEY (user_id, cluster_id)
);

-- Il fan-out inverso: dato un cluster, chi lo segue. È l'indice su cui gira il
-- worker, che itera sui cluster e non sugli utenti.
CREATE INDEX user_clusters_by_cluster_idx ON user_clusters (cluster_id)
  WHERE NOT is_paused;

COMMENT ON COLUMN user_clusters.company_sizes IS
  'Non applicabile: il fornitore non espone la dimensione azienda. Vedi CLAUDE.md.';


-- min <= max non è esprimibile come CHECK, perché il confronto passa dai rank
-- in experience_levels e le CHECK non ammettono sottoquery.
CREATE OR REPLACE FUNCTION assert_seniority_order() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_min smallint;
  v_max smallint;
BEGIN
  IF NEW.min_seniority IS NULL OR NEW.max_seniority IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT rank INTO v_min FROM experience_levels WHERE code = NEW.min_seniority;
  SELECT rank INTO v_max FROM experience_levels WHERE code = NEW.max_seniority;
  IF v_min > v_max THEN
    RAISE EXCEPTION 'min_seniority (%) è superiore a max_seniority (%)',
      NEW.min_seniority, NEW.max_seniority
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER user_clusters_seniority_order
  BEFORE INSERT OR UPDATE OF min_seniority, max_seniority ON user_clusters
  FOR EACH ROW EXECUTE FUNCTION assert_seniority_order();
