-- 0089 — Cache vettoriale E5 per il retrieval match-v3.
-- Una riga per offerta e modello: l'hash del renderer impedisce il riuso
-- di vettori calcolati su contenuto obsoleto.

CREATE TABLE IF NOT EXISTS job_embeddings (
  job_id        uuid        NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
  model         text        NOT NULL CHECK (model <> ''),
  content_hash  text        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
  embedding     vector(1024) NOT NULL,
  embedded_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, model)
);

ALTER TABLE job_embeddings
  ADD COLUMN IF NOT EXISTS content_hash text,
  ADD COLUMN IF NOT EXISTS embedded_at timestamptz;

UPDATE job_embeddings SET content_hash = md5(
  COALESCE((SELECT j.title || coalesce(j.ai_requirements_summary, '') ||
            coalesce(j.ai_core_responsibilities, '') FROM jobs j WHERE j.id = job_embeddings.job_id), '')
) WHERE content_hash IS NULL;

ALTER TABLE job_embeddings
  ALTER COLUMN content_hash SET NOT NULL,
  ALTER COLUMN embedded_at SET DEFAULT now();

ALTER TABLE job_embeddings DROP CONSTRAINT IF EXISTS job_embeddings_pkey;
ALTER TABLE job_embeddings ADD PRIMARY KEY (job_id, model);

COMMENT ON TABLE job_embeddings IS
  'Embedding E5 cache match-v3; upsert solo se cambia contenuto o modello.';

CREATE INDEX IF NOT EXISTS job_embeddings_hnsw_idx ON job_embeddings
  USING hnsw (embedding vector_cosine_ops);
