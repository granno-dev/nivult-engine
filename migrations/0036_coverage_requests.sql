-- 0036 — Quello che ci chiedono e non copriamo ancora.
--
-- Un cluster nasce quando cominciamo a scaricarlo, quindi l'onboarding può
-- offrire solo ciò che esiste già. Fin qui è corretto: una casella senza
-- corpus dietro darebbe un digest vuoto, e un digest vuoto si legge come
-- "non c'è lavoro per me" invece che come "quel mercato non lo leggiamo".
--
-- Sbagliato era non dirlo. Chi arrivava cercando la Spagna trovava tre
-- riquadri e nessuna spiegazione, e la sua richiesta spariva — mentre è
-- l'informazione più preziosa che abbiamo in questa fase: quale cluster
-- accendere dopo. La domanda va raccolta dove nasce.
CREATE TABLE coverage_requests (
  id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),

  -- Come api_usage: la richiesta sopravvive alla cancellazione dell'utente,
  -- l'attribuzione a una persona no. Il segnale di domanda è nostro e non
  -- contiene nulla di personale; chi l'ha chiesto è un dato dell'utente.
  user_id    uuid        REFERENCES users(id) ON DELETE SET NULL,

  kind       text        NOT NULL CHECK (kind IN ('cluster', 'language')),

  -- Per kind='cluster'. `family` non ha FK verso job_families di proposito:
  -- qui si raccoglie anche ciò che la tassonomia del fornitore non ha, ed è
  -- esattamente il caso che vogliamo vedere.
  family     text,
  country    char(2)     CHECK (country IS NULL OR country ~ '^[A-Z]{2}$'),

  -- Per kind='language'.
  language   text,

  created_at timestamptz NOT NULL DEFAULT now(),

  -- Ogni tipo porta i suoi campi e non quelli dell'altro: una riga a metà
  -- non si conta, e contare è tutto il senso di questa tabella.
  CONSTRAINT coverage_requests_shape_ck CHECK (
    (kind = 'cluster'  AND family IS NOT NULL AND country IS NOT NULL
                       AND language IS NULL) OR
    (kind = 'language' AND language IS NOT NULL
                       AND family IS NULL AND country IS NULL)
  )
);

CREATE INDEX coverage_requests_cluster_idx
  ON coverage_requests (country, family) WHERE kind = 'cluster';
CREATE INDEX coverage_requests_language_idx
  ON coverage_requests (language) WHERE kind = 'language';

COMMENT ON TABLE coverage_requests IS
  'Cosa ci viene chiesto e non copriamo. Si legge per decidere quale cluster '
  'accendere: e'' domanda misurata, non ipotizzata.';

-- La vista che si guarda per decidere: la domanda ordinata per volume, senza
-- nessun riferimento a chi l'ha espressa.
CREATE VIEW coverage_demand_v AS
SELECT kind,
       COALESCE(family, language) AS cosa,
       country,
       count(*)                   AS richieste,
       min(created_at)            AS prima_richiesta,
       max(created_at)            AS ultima_richiesta
FROM coverage_requests
GROUP BY kind, COALESCE(family, language), country
ORDER BY count(*) DESC;

COMMENT ON VIEW coverage_demand_v IS
  'Domanda non coperta, per volume. Nessun user_id: serve a decidere cosa '
  'aprire, non a sapere chi l''ha chiesto.';
