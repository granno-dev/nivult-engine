-- 0083: provenienza reale delle inferenze e guasti separati per canale.

ALTER TABLE api_usage ADD COLUMN model text;
ALTER TABLE api_usage ADD COLUMN cached_tokens integer CHECK (cached_tokens >= 0);
ALTER TABLE api_usage ALTER COLUMN cost_micros DROP NOT NULL;
ALTER TABLE api_usage ALTER COLUMN cost_micros SET DEFAULT NULL;

ALTER TABLE api_usage DROP CONSTRAINT api_usage_provider_check;
ALTER TABLE api_usage ADD CONSTRAINT api_usage_provider_check CHECK (
    provider = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                          'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                          'glm', 'bge-m3', 'bge-reranker', 'ats',
                          'linkedin', 'indeed', 'groq', 'nvidia', 'mistral',
                          'maestro-locale', 'scrittore-locale'])
);

ALTER TABLE api_usage DROP CONSTRAINT api_usage_operation_check;
ALTER TABLE api_usage ADD CONSTRAINT api_usage_operation_check CHECK (
    operation IN ('fetch', 'embed', 'rerank', 'score', 'motivation')
);

CREATE INDEX api_usage_model_idx ON api_usage (model, occurred_at DESC)
WHERE model IS NOT NULL;

COMMENT ON COLUMN api_usage.model IS
  'Nome del modello effettivamente usato; NULL per operazioni che non sono inferenza.';
COMMENT ON COLUMN api_usage.cached_tokens IS
  'Token di input serviti dalla cache, necessari per calcolare il costo reale.';
COMMENT ON COLUMN api_usage.cost_micros IS
  'Costo in micros di dollaro; NULL se prezzo o uso token non sono noti.';

ALTER TABLE matches ADD COLUMN provider text;
ALTER TABLE matches ADD COLUMN analysis_model text;
ALTER TABLE matches ADD COLUMN analysis_provider text;
ALTER TABLE matches ADD COLUMN analysis_input_tokens integer
  CHECK (analysis_input_tokens >= 0);
ALTER TABLE matches ADD COLUMN analysis_output_tokens integer
  CHECK (analysis_output_tokens >= 0);

COMMENT ON COLUMN matches.provider IS
  'Provider che ha prodotto il punteggio; il modello resta in matches.model.';
COMMENT ON COLUMN matches.analysis_model IS
  'Modello effettivamente usato per motivazione e analisi nella seconda passata.';
COMMENT ON COLUMN matches.analysis_provider IS
  'Provider effettivamente usato per motivazione e analisi nella seconda passata.';

ALTER TABLE users ADD COLUMN delivery_failures_by_channel jsonb NOT NULL DEFAULT '{}';
ALTER TABLE users ADD CONSTRAINT users_delivery_failures_by_channel_ck CHECK (
  jsonb_typeof(delivery_failures_by_channel) = 'object'
);

COMMENT ON COLUMN users.delivery_failures_by_channel IS
  'Contatori interi per email, telegram, whatsapp e app; il worker incrementa e azzera solo il canale interessato.';
