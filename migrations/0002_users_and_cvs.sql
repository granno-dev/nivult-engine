-- 0002 — Utenti, preferenze di consegna, CV.

-- Vocabolario dei livelli di esperienza.
-- Serve a dare un ORDINE ai filtri min/max dell'utente: senza rank, "da mid a
-- senior" non è esprimibile in SQL.
-- ATTENZIONE: i codici qui sotto sono PROVVISORI, ipotizzati prima di aver
-- visto una risposta reale di Fantastic.jobs. Vanno confermati alla prima
-- ingestione — scripts/verify_schema.py segnala i valori di
-- jobs.ai_experience_level che non compaiono in questa tabella.
CREATE TABLE experience_levels (
  code  text     PRIMARY KEY,
  rank  smallint NOT NULL UNIQUE,
  label text     NOT NULL
);

INSERT INTO experience_levels (code, rank, label) VALUES
  ('0-2',  1, 'Entry / junior'),
  ('2-5',  2, 'Mid'),
  ('5-10', 3, 'Senior'),
  ('10+',  4, 'Lead / principal');

COMMENT ON TABLE experience_levels IS
  'Vocabolario PROVVISORIO di ai_experience_level. Confermare con dati reali.';


CREATE TABLE users (
  id                      uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  email                   citext      NOT NULL,
  created_at              timestamptz NOT NULL DEFAULT now(),
  updated_at              timestamptz NOT NULL DEFAULT now(),
  timezone                text        NOT NULL DEFAULT 'UTC',

  plan                    text        NOT NULL
                                      CHECK (plan IN ('basic','pro','ultra')),
  subscription_status     text        NOT NULL
                                      CHECK (subscription_status IN
                                        ('trialing','active','past_due','canceled','expired')),
  current_period_end      timestamptz,
  billing_customer_id     text,
  billing_subscription_id text,

  delivery_channel        text        NOT NULL
                                      CHECK (delivery_channel IN ('email','telegram','whatsapp')),
  delivery_email          citext,
  telegram_chat_id        text,
  whatsapp_e164           text        CHECK (whatsapp_e164 ~ '^\+[1-9][0-9]{6,14}$'),
  channel_verified_at     timestamptz,

  frequency               text        NOT NULL
                                      CHECK (frequency IN ('daily','weekly','monthly')),
  send_hour_local         smallint    NOT NULL DEFAULT 8
                                      CHECK (send_hour_local BETWEEN 0 AND 23),
  send_weekday            smallint    CHECK (send_weekday BETWEEN 1 AND 7),
  send_monthday           smallint    CHECK (send_monthday BETWEEN 1 AND 28),
  next_digest_at          timestamptz,
  last_digest_at          timestamptz,

  status                  text        NOT NULL DEFAULT 'active'
                                      CHECK (status IN ('active','paused','deleted')),
  deleted_at              timestamptz,

  -- Il canale scelto deve avere il suo recapito. email è NOT NULL, quindi il
  -- ramo 'email' è sempre soddisfatto.
  CONSTRAINT users_channel_address_ck CHECK (
       (delivery_channel = 'email')
    OR (delivery_channel = 'telegram' AND telegram_chat_id IS NOT NULL)
    OR (delivery_channel = 'whatsapp' AND whatsapp_e164   IS NOT NULL)
  ),

  -- I campi di pianificazione devono corrispondere alla frequenza: niente
  -- send_weekday su un digest giornaliero, niente weekly senza giorno.
  CONSTRAINT users_schedule_ck CHECK (
       (frequency = 'daily'   AND send_weekday IS NULL     AND send_monthday IS NULL)
    OR (frequency = 'weekly'  AND send_weekday IS NOT NULL AND send_monthday IS NULL)
    OR (frequency = 'monthly' AND send_weekday IS NULL     AND send_monthday IS NOT NULL)
  ),

  CONSTRAINT users_deleted_ck CHECK ((status = 'deleted') = (deleted_at IS NOT NULL))
);

CREATE UNIQUE INDEX users_email_key ON users (email);

-- La coda dello scheduler. Parziale, perché gli utenti non attivi non vengono
-- mai interrogati e non devono appesantire l'indice.
CREATE INDEX users_due_idx ON users (next_digest_at)
  WHERE status = 'active' AND next_digest_at IS NOT NULL;

CREATE TRIGGER users_set_updated_at
  BEFORE UPDATE ON users
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();


-- Un fuso orario sbagliato non si scopre al momento dell'INSERT ma sei ore
-- dopo, quando lo scheduler calcola l'orario di invio. Meglio rifiutarlo qui.
-- Non è una CHECK perché la validazione richiede il catalogo dei fusi, che non
-- è IMMUTABLE.
CREATE OR REPLACE FUNCTION assert_valid_timezone() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    PERFORM now() AT TIME ZONE NEW.timezone;
  EXCEPTION WHEN OTHERS THEN
    RAISE EXCEPTION 'fuso orario non valido: %', NEW.timezone
      USING ERRCODE = 'check_violation';
  END;
  RETURN NEW;
END $$;

CREATE TRIGGER users_valid_timezone
  BEFORE INSERT OR UPDATE OF timezone ON users
  FOR EACH ROW EXECUTE FUNCTION assert_valid_timezone();


-- Il CV sta in una tabella sua e non in colonne di users, perché è IL dato
-- personale da cancellare: isolarlo rende la cancellazione un'operazione su
-- una tabella invece che una caccia fra le colonne.
-- Il file non entra in Postgres: qui c'è solo il puntatore all'object storage.
CREATE TABLE user_cvs (
  id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id           uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  status            text        NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','superseded')),
  uploaded_at       timestamptz NOT NULL DEFAULT now(),
  parsed_at         timestamptz,

  storage_key       text        NOT NULL,
  original_filename text,
  mime_type         text,
  sha256            char(64)    CHECK (sha256 ~ '^[0-9a-f]{64}$'),

  -- Profilo strutturato estratto dall'AI.
  families          text[]      NOT NULL DEFAULT '{}',
  seniority         text        REFERENCES experience_levels(code),
  skills            text[]      NOT NULL DEFAULT '{}',
  languages         jsonb       NOT NULL DEFAULT '[]',
  years_experience  smallint    CHECK (years_experience BETWEEN 0 AND 70),
  raw_extraction    jsonb,

  embedding         vector(1024),
  embedding_model   text,

  CONSTRAINT user_cvs_embedding_ck CHECK ((embedding IS NULL) = (embedding_model IS NULL))
);

-- Un solo CV attivo per utente. Un nuovo caricamento degrada il precedente a
-- 'superseded' invece di sovrascriverlo, così resta ricostruibile quale
-- versione del CV ha prodotto un dato match.
CREATE UNIQUE INDEX user_cvs_one_active_idx ON user_cvs (user_id) WHERE status = 'active';
CREATE INDEX user_cvs_user_idx ON user_cvs (user_id);

COMMENT ON TABLE user_cvs IS
  'Dato personale. Il testo del CV non va MAI nei log. Cancellazione: nivult.gdpr.';
COMMENT ON COLUMN user_cvs.storage_key IS
  'Chiave object storage. Il blob va cancellato insieme alla riga.';
