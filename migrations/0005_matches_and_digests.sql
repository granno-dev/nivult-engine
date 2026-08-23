-- 0005 — Valutazioni dell'LLM e digest inviati.

-- TABELLA NON PARTIZIONATA, per scelta.
--
-- In Postgres una UNIQUE su tabella partizionata deve contenere tutte le colonne
-- della chiave di partizionamento. Partizionando per mese su evaluated_at,
-- l'unica UNIQUE ammessa sarebbe (user_id, job_id, evaluated_at) — che consente
-- esattamente ciò che dobbiamo vietare: la stessa offerta rimandata allo stesso
-- utente il mese dopo. Un vincolo che sembra protettivo e non protegge nulla è
-- peggio di nessun vincolo.
-- La migrazione futura è documentata in CLAUDE.md.
CREATE TABLE matches (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id        uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  -- RESTRICT: un'offerta già valutata non si cancella. La retention delle
  -- offerte deve rispettare lo storico, non scavalcarlo in silenzio.
  job_id         uuid        NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
  -- Quale versione del CV ha prodotto il giudizio. SET NULL e non CASCADE: se il
  -- CV viene sostituito la valutazione resta valida.
  cv_id          uuid        REFERENCES user_cvs(id) ON DELETE SET NULL,

  score          smallint    NOT NULL CHECK (score BETWEEN 0 AND 100),
  reason         text        NOT NULL,
  threshold_used smallint    NOT NULL CHECK (threshold_used BETWEEN 0 AND 100),
  -- Colonna generata: non può divergere dal punteggio.
  passed         boolean     GENERATED ALWAYS AS (score >= threshold_used) STORED,

  model          text        NOT NULL,
  evaluated_at   timestamptz NOT NULL DEFAULT now(),
  input_tokens   integer,
  output_tokens  integer,

  -- L'anti-ripetizione. Un'offerta valutata per un utente non può essere
  -- rivalutata né riproposta, e il tentativo fallisce in database invece di
  -- passare inosservato.
  CONSTRAINT matches_user_job_key UNIQUE (user_id, job_id),
  -- Serve alla FK composita di digest_items, che garantisce che una voce di
  -- digest non possa puntare al match di un altro utente.
  CONSTRAINT matches_id_user_key  UNIQUE (id, user_id)
);

CREATE INDEX matches_user_recent_idx ON matches (user_id, evaluated_at DESC);
CREATE INDEX matches_passed_idx ON matches (user_id, evaluated_at DESC) WHERE passed;
CREATE INDEX matches_job_idx ON matches (job_id);

COMMENT ON TABLE matches IS
  'Una riga per ogni coppia utente-offerta valutata dall LLM, scarti compresi: '
  'è proprio lo scarto che non vogliamo ri-pagare domani.';


CREATE TABLE digests (
  id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  -- Snapshot del canale al momento dell'invio: se l'utente cambia canale
  -- domani, resta ricostruibile dove è arrivato questo.
  channel              text        NOT NULL
                                   CHECK (channel IN ('email','telegram','whatsapp')),
  scheduled_for        timestamptz NOT NULL,
  period_start         timestamptz,
  period_end           timestamptz,

  started_at           timestamptz,
  sent_at              timestamptz,
  status               text        NOT NULL DEFAULT 'pending'
                                   CHECK (status IN ('pending','sent','failed','skipped_empty')),

  jobs_evaluated_count integer     NOT NULL DEFAULT 0 CHECK (jobs_evaluated_count >= 0),
  jobs_sent_count      integer     NOT NULL DEFAULT 0 CHECK (jobs_sent_count >= 0),

  provider_message_id  text,
  error_message        text,
  attempt_count        integer     NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),

  -- Idempotenza: un utente non può ricevere due volte il digest della stessa
  -- finestra, nemmeno se il worker viene rilanciato.
  CONSTRAINT digests_user_slot_key UNIQUE (user_id, scheduled_for),
  CONSTRAINT digests_id_user_key   UNIQUE (id, user_id),

  CONSTRAINT digests_sent_ck    CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
  -- skipped_empty è uno stato di prima classe, non un fallimento: se nessuna
  -- offerta supera la soglia non mandiamo nulla, e deve restare distinguibile
  -- da una consegna rotta.
  CONSTRAINT digests_empty_ck   CHECK (status <> 'skipped_empty' OR jobs_sent_count = 0),
  CONSTRAINT digests_counts_ck  CHECK (jobs_sent_count <= jobs_evaluated_count),
  CONSTRAINT digests_period_ck  CHECK (period_start IS NULL OR period_end IS NULL
                                       OR period_start < period_end)
);

CREATE INDEX digests_user_recent_idx ON digests (user_id, scheduled_for DESC);
CREATE INDEX digests_pending_idx ON digests (scheduled_for)
  WHERE status IN ('pending','failed');


CREATE TABLE digest_items (
  digest_id       uuid     NOT NULL,
  job_id          uuid     NOT NULL REFERENCES jobs(id) ON DELETE RESTRICT,
  -- Ridondante rispetto a digests.user_id, ed è il prezzo delle due FK
  -- composite qui sotto.
  user_id         uuid     NOT NULL,
  match_id        uuid     NOT NULL,
  rank            smallint NOT NULL CHECK (rank > 0),

  -- Snapshot volutamente ridondanti: ricostruire cosa ha ricevuto un utente
  -- deve restare possibile anche se un domani rivalutiamo o correggiamo i match.
  score_snapshot  smallint NOT NULL CHECK (score_snapshot BETWEEN 0 AND 100),
  reason_snapshot text     NOT NULL,

  PRIMARY KEY (digest_id, job_id),
  CONSTRAINT digest_items_rank_key UNIQUE (digest_id, rank),

  -- Coerenza dichiarativa invece che via trigger: una voce di digest non può
  -- puntare al match di un altro utente, perché user_id deve combaciare in
  -- entrambe le chiavi.
  CONSTRAINT digest_items_digest_fk FOREIGN KEY (digest_id, user_id)
    REFERENCES digests (id, user_id) ON DELETE CASCADE,
  CONSTRAINT digest_items_match_fk  FOREIGN KEY (match_id, user_id)
    REFERENCES matches (id, user_id) ON DELETE CASCADE
);

CREATE INDEX digest_items_job_idx ON digest_items (job_id);
CREATE INDEX digest_items_user_idx ON digest_items (user_id);
