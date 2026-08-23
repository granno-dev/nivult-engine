-- 0006 — Esecuzioni di ingestione e ledger dei consumi.

CREATE TABLE ingestion_runs (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  cluster_id     uuid        NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  source         text        NOT NULL CHECK (source IN (
                               'fantastic','france_travail','bundesagentur',
                               'arbetsformedlingen','nav','tyomarkkinatori')),
  started_at     timestamptz NOT NULL DEFAULT now(),
  finished_at    timestamptz,
  status         text        NOT NULL DEFAULT 'running'
                             CHECK (status IN ('running','success','failed','aborted_budget')),
  -- I parametri effettivi della chiamata (title/location/time_frame/limit):
  -- senza questi, un risultato anomalo non è diagnosticabile a posteriori.
  request_params jsonb       NOT NULL DEFAULT '{}',

  jobs_fetched   integer     NOT NULL DEFAULT 0 CHECK (jobs_fetched >= 0),
  jobs_new       integer     NOT NULL DEFAULT 0 CHECK (jobs_new >= 0),
  jobs_updated   integer     NOT NULL DEFAULT 0 CHECK (jobs_updated >= 0),
  -- Quante offerte scartiamo perché il link non porta a un career site
  -- aziendale. È la metrica che dice se una fonte sta peggiorando.
  jobs_rejected_intermediary integer NOT NULL DEFAULT 0
                             CHECK (jobs_rejected_intermediary >= 0),

  error_message  text,

  CONSTRAINT ingestion_runs_finished_ck
    CHECK ((status = 'running') = (finished_at IS NULL)),
  CONSTRAINT ingestion_runs_failed_ck
    CHECK (status <> 'failed' OR error_message IS NOT NULL)
);

CREATE INDEX ingestion_runs_cluster_idx ON ingestion_runs (cluster_id, started_at DESC);
CREATE INDEX ingestion_runs_running_idx ON ingestion_runs (started_at) WHERE status = 'running';

ALTER TABLE job_clusters
  ADD CONSTRAINT job_clusters_run_fk
  FOREIGN KEY (ingestion_run_id) REFERENCES ingestion_runs(id) ON DELETE SET NULL;


-- Ledger unico per ingestione e inferenza.
--
-- Non due tabelle separate: sono la stessa domanda — "quanto ci costa davvero
-- questo cluster / questo utente" — e tenerla in due posti significa fare UNION
-- ogni volta. cluster_daily_budget è l'aggregato veloce che legge il circuit
-- breaker; questa è la verità di dettaglio da cui si può sempre ricostruire.
--
-- Tabella piana e non partizionata per mese: la retention si fa a DELETE. Il
-- partizionamento qui sarebbe stato gratuito, ma introduce un job di
-- manutenzione che se salta fa fallire gli INSERT — e questa tabella è sul
-- percorso di ogni chiamata API.
CREATE TABLE api_usage (
  id               bigserial   PRIMARY KEY,
  occurred_at      timestamptz NOT NULL DEFAULT now(),
  provider         text        NOT NULL CHECK (provider IN (
                                 'fantastic','france_travail','bundesagentur',
                                 'arbetsformedlingen','nav','tyomarkkinatori',
                                 'glm','bge-m3','bge-reranker')),
  operation        text        NOT NULL CHECK (operation IN ('fetch','embed','rerank','score')),

  -- Entrambi nullable: non ogni chiamata appartiene a un cluster e a un utente.
  cluster_id       uuid        REFERENCES clusters(id) ON DELETE SET NULL,
  -- SET NULL e non CASCADE: alla cancellazione di un utente lo storico dei costi
  -- sopravvive, il collegamento con la persona no.
  user_id          uuid        REFERENCES users(id) ON DELETE SET NULL,
  ingestion_run_id uuid        REFERENCES ingestion_runs(id) ON DELETE SET NULL,

  requests         integer     NOT NULL DEFAULT 1 CHECK (requests >= 0),
  credits          integer     NOT NULL DEFAULT 0 CHECK (credits >= 0),
  input_tokens     integer     CHECK (input_tokens >= 0),
  output_tokens    integer     CHECK (output_tokens >= 0),
  cost_micros      bigint      NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),

  http_status      integer,
  latency_ms       integer     CHECK (latency_ms >= 0)
);

CREATE INDEX api_usage_cluster_idx  ON api_usage (cluster_id, occurred_at DESC)
  WHERE cluster_id IS NOT NULL;
CREATE INDEX api_usage_user_idx     ON api_usage (user_id, occurred_at DESC)
  WHERE user_id IS NOT NULL;
CREATE INDEX api_usage_provider_idx ON api_usage (provider, occurred_at DESC);
CREATE INDEX api_usage_time_idx     ON api_usage (occurred_at);

COMMENT ON COLUMN api_usage.cost_micros IS 'Costo in milionesimi di euro. Intero: niente float sui soldi.';
