-- 0030 — Confronto fra modelli di valutazione.
--
-- In tabella e non in un file di risultati: quando cambieremo modello vogliamo
-- poter rifare lo stesso confronto sulle stesse offerte e mettere i numeri
-- accanto a quelli vecchi, non ricominciare da capo.

CREATE TABLE model_pricing (
  model              text PRIMARY KEY,
  input_per_mtok     numeric(10,4),
  output_per_mtok    numeric(10,4),
  cached_per_mtok    numeric(10,4),
  note               text,
  updated_at         timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE model_pricing IS
  'Prezzi per milione di token. NULL = prezzo non confermato: il costo non si '
  'calcola, ma i token restano misurati e il conto si completa dopo.';

INSERT INTO model_pricing (model, input_per_mtok, output_per_mtok, note) VALUES
  ('glm-5.2',        NULL, NULL, 'prezzo da confermare'),
  ('glm-4.7-flashx', 0.07, 0.40, 'dichiarato'),
  ('glm-4.7-flash',  0.00, 0.00, 'gratuito');

CREATE TABLE benchmark_runs (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  label        text        NOT NULL,
  run_at       timestamptz NOT NULL DEFAULT now(),
  job_count    integer     NOT NULL CHECK (job_count > 0),
  profile_hash char(64)    NOT NULL CHECK (profile_hash ~ '^[0-9a-f]{64}$'),
  prompt_hash  char(64)    NOT NULL CHECK (prompt_hash ~ '^[0-9a-f]{64}$'),
  reference_model text     NOT NULL,
  note         text
);

COMMENT ON COLUMN benchmark_runs.profile_hash IS
  'sha256 del profilo. Serve a sapere se due confronti sono paragonabili, '
  'senza conservare il CV.';

CREATE TABLE benchmark_models (
  id             uuid     PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id         uuid     NOT NULL REFERENCES benchmark_runs(id) ON DELETE CASCADE,
  model          text     NOT NULL,
  input_tokens   integer  NOT NULL DEFAULT 0,
  cached_tokens  integer  NOT NULL DEFAULT 0,
  output_tokens  integer  NOT NULL DEFAULT 0,
  elapsed_s      numeric(10,1),
  calls_ok       integer  NOT NULL DEFAULT 0,
  calls_failed   integer  NOT NULL DEFAULT 0,
  rate_limited   integer  NOT NULL DEFAULT 0,
  concurrency    smallint,
  CONSTRAINT benchmark_models_run_model_key UNIQUE (run_id, model)
);

COMMENT ON COLUMN benchmark_models.rate_limited IS
  'Quante volte la fonte ha risposto 429. Su un modello gratuito e un risultato '
  'da misurare, non un ostacolo da aggirare.';

CREATE TABLE benchmark_scores (
  model_run_id uuid     NOT NULL REFERENCES benchmark_models(id) ON DELETE CASCADE,
  job_id       uuid     NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  score        smallint CHECK (score BETWEEN 0 AND 100),
  reason       text,
  latency_ms   integer,
  error        text,
  PRIMARY KEY (model_run_id, job_id),
  CONSTRAINT benchmark_scores_esito_ck
    CHECK ((score IS NULL) <> (error IS NULL))
);

CREATE INDEX benchmark_scores_ranking_idx
  ON benchmark_scores (model_run_id, score DESC NULLS LAST);

CREATE VIEW benchmark_models_v AS
SELECT m.*, r.label, r.run_at, r.job_count,
       CASE WHEN p.input_per_mtok IS NULL THEN NULL ELSE
         round((m.input_tokens - m.cached_tokens) / 1e6 * p.input_per_mtok
             + m.cached_tokens / 1e6 * COALESCE(p.cached_per_mtok, p.input_per_mtok)
             + m.output_tokens  / 1e6 * p.output_per_mtok, 5)
       END AS cost_usd,
       CASE WHEN m.input_tokens > 0
            THEN round(100.0 * m.cached_tokens / m.input_tokens, 1) END AS cache_pct
FROM benchmark_models m
JOIN benchmark_runs r ON r.id = m.run_id
LEFT JOIN model_pricing p ON p.model = m.model;

CREATE OR REPLACE FUNCTION benchmark_recall(
  p_run_id uuid, p_top_reference integer DEFAULT 20, p_top_candidate integer DEFAULT 30)
RETURNS TABLE (model text, trovate bigint, su bigint, recall_pct numeric)
LANGUAGE sql STABLE AS $$
  WITH rif AS (
    SELECT s.job_id
      FROM benchmark_scores s
      JOIN benchmark_models m ON m.id = s.model_run_id
      JOIN benchmark_runs r   ON r.id = m.run_id
     WHERE m.run_id = p_run_id AND m.model = r.reference_model AND s.score IS NOT NULL
     ORDER BY s.score DESC, s.job_id
     LIMIT p_top_reference
  ),
  cand AS (
    SELECT m.model, s.job_id,
           row_number() OVER (PARTITION BY m.model ORDER BY s.score DESC, s.job_id) AS pos
      FROM benchmark_scores s
      JOIN benchmark_models m ON m.id = s.model_run_id
     WHERE m.run_id = p_run_id AND s.score IS NOT NULL
  )
  SELECT c.model,
         count(*) FILTER (WHERE c.pos <= p_top_candidate)::bigint,
         (SELECT count(*) FROM rif)::bigint,
         round(100.0 * count(*) FILTER (WHERE c.pos <= p_top_candidate)
               / NULLIF((SELECT count(*) FROM rif), 0), 1)
    FROM cand c JOIN rif ON rif.job_id = c.job_id
   GROUP BY c.model
   ORDER BY 4 DESC NULLS LAST;
$$;
