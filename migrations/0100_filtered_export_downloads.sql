-- 0100 — Filtered export recuperabili dal proprietario senza nuovi addebiti.

CREATE TABLE portale_export_files (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  usage_id    bigint NOT NULL UNIQUE REFERENCES portale_uso(id) ON DELETE CASCADE,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  percorso    text NOT NULL,
  filename    text NOT NULL,
  tipo        text NOT NULL CHECK (tipo IN ('postings', 'companies')),
  righe       integer NOT NULL CHECK (righe > 0),
  filtri      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX portale_export_files_user_idx
  ON portale_export_files (user_id, created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON portale_export_files TO nivult_app;
