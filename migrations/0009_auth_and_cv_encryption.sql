-- 0009 — Autenticazione senza password e cifratura lato client dei CV.
--
-- Decisione: magic-link via email + OAuth Google/Microsoft. NESSUNA password.
-- Non c'è una colonna password da proteggere, un reset da mettere in sicurezza,
-- né un riuso di credenziali che ci possa danneggiare. È la classe di problemi
-- che non esiste, non quella che abbiamo risolto bene.

-- Il magic link prova il possesso dell'indirizzo.
ALTER TABLE users ADD COLUMN email_verified_at timestamptz;


-- Token di accesso monouso.
--
-- Si conserva SOLO lo sha256 del token, mai il token. Se il database trapela,
-- quello che l'attaccante trova non permette di autenticarsi. sha256 nudo basta
-- e avanza: il token è generato da noi con entropia alta, non è una password
-- indovinabile, quindi non serve una KDF costosa.
CREATE TABLE login_tokens (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   char(64)    NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  consumed_at  timestamptz,
  -- Dati di richiesta: servono a riconoscere un abuso. Sono dati personali,
  -- quindi seguono l'utente nella cancellazione.
  requested_ip inet,
  requested_ua text,

  CONSTRAINT login_tokens_hash_key   UNIQUE (token_hash),
  CONSTRAINT login_tokens_window_ck  CHECK (expires_at > created_at),
  CONSTRAINT login_tokens_consumed_ck
    CHECK (consumed_at IS NULL OR consumed_at >= created_at)
);

CREATE INDEX login_tokens_user_idx ON login_tokens (user_id, created_at DESC);
-- Per la potatura e per il conteggio delle richieste recenti (rate limiting).
CREATE INDEX login_tokens_expiry_idx ON login_tokens (expires_at)
  WHERE consumed_at IS NULL;

COMMENT ON COLUMN login_tokens.token_hash IS
  'sha256 del token. Il token in chiaro non entra MAI nel database né nei log.';


-- Sessioni: cosa il magic link produce una volta consumato.
CREATE TABLE sessions (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id       uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash    char(64)    NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  last_seen_at  timestamptz,
  revoked_at    timestamptz,
  -- Come è nata la sessione: utile quando si indaga su un accesso sospetto.
  origin        text        NOT NULL CHECK (origin IN ('magic_link','google','microsoft')),
  ip            inet,
  user_agent    text,

  CONSTRAINT sessions_hash_key  UNIQUE (token_hash),
  CONSTRAINT sessions_window_ck CHECK (expires_at > created_at)
);

CREATE INDEX sessions_user_idx ON sessions (user_id, created_at DESC);
CREATE INDEX sessions_active_idx ON sessions (expires_at)
  WHERE revoked_at IS NULL;


-- Identità OAuth.
--
-- La chiave è (provider, subject) e NON l'email. Il claim 'sub' è l'unico
-- identificativo stabile che i provider garantiscono: le email cambiano, e
-- alcuni domini le riassegnano a persone diverse. Agganciare l'account
-- all'email significherebbe che chi eredita un indirizzo eredita l'account.
CREATE TABLE oauth_identities (
  provider      text        NOT NULL CHECK (provider IN ('google','microsoft')),
  subject       text        NOT NULL,
  user_id       uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  email_at_link citext,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_login_at timestamptz,

  PRIMARY KEY (provider, subject),
  -- Un solo account per provider per utente.
  CONSTRAINT oauth_identities_user_provider_key UNIQUE (user_id, provider)
);

CREATE INDEX oauth_identities_user_idx ON oauth_identities (user_id);

COMMENT ON COLUMN oauth_identities.email_at_link IS
  'Email al momento del collegamento, a scopo diagnostico. NON è la chiave.';


-- Cifratura lato client dei CV, con schema a busta.
--
-- Ogni file ha la sua chiave (DEK) casuale; la DEK viene avvolta con la chiave
-- master (KEK) che sta in variabile d'ambiente sul server. Hetzner riceve solo
-- byte opachi: non ha né la KEK né le DEK.
--
-- Il motivo della busta è la rotazione: cambiare la KEK significa riavvolgere
-- delle DEK di poche decine di byte, non riscaricare e ricifrare ogni CV.
-- kek_version dice con quale generazione di KEK è avvolta una data DEK, così la
-- rotazione può essere incrementale invece che atomica.
ALTER TABLE user_cvs
  ADD COLUMN encryption_algo text     NOT NULL DEFAULT 'aes-256-gcm',
  ADD COLUMN encrypted_dek   bytea    NOT NULL DEFAULT '\x',
  ADD COLUMN nonce           bytea    NOT NULL DEFAULT '\x',
  ADD COLUMN auth_tag        bytea    NOT NULL DEFAULT '\x',
  ADD COLUMN kek_version     smallint NOT NULL DEFAULT 1;

-- I DEFAULT servivano solo a poter aggiungere colonne NOT NULL; da qui in poi
-- ogni riga deve portare i propri valori reali.
ALTER TABLE user_cvs
  ALTER COLUMN encryption_algo DROP DEFAULT,
  ALTER COLUMN encrypted_dek   DROP DEFAULT,
  ALTER COLUMN nonce           DROP DEFAULT,
  ALTER COLUMN auth_tag        DROP DEFAULT,
  ALTER COLUMN kek_version     DROP DEFAULT;

ALTER TABLE user_cvs
  ADD CONSTRAINT user_cvs_encryption_ck CHECK (
        encryption_algo = 'aes-256-gcm'
    AND length(encrypted_dek) >= 32
    AND length(nonce)         = 12
    AND length(auth_tag)      = 16
    AND kek_version > 0
  );

COMMENT ON COLUMN user_cvs.encrypted_dek IS
  'DEK avvolta con la KEK di kek_version. La KEK non entra MAI nel database.';


-- La cancellazione GDPR deve coprire anche le tabelle nuove.
--
-- Le FK sono ON DELETE CASCADE, quindi senza questo aggiornamento le righe
-- sparirebbero comunque — ma tutte insieme, nella transazione del DELETE finale
-- su users, che è esattamente il lock lungo che il lotto serve a evitare.
-- E sono dati personali veri: indirizzi IP, user agent, email dei provider.
CREATE OR REPLACE FUNCTION delete_user_batch(p_user_id uuid, p_batch_size integer DEFAULT 5000)
RETURNS TABLE (step text, rows_affected bigint)
LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
BEGIN
  IF p_batch_size <= 0 THEN
    RAISE EXCEPTION 'p_batch_size deve essere positivo, ricevuto %', p_batch_size;
  END IF;

  DELETE FROM digest_items WHERE ctid IN (
    SELECT ctid FROM digest_items WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'digest_items'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM digests WHERE ctid IN (
    SELECT ctid FROM digests WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'digests'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM matches WHERE ctid IN (
    SELECT ctid FROM matches WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'matches'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM sessions WHERE ctid IN (
    SELECT ctid FROM sessions WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'sessions'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM login_tokens WHERE ctid IN (
    SELECT ctid FROM login_tokens WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'login_tokens'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM oauth_identities WHERE ctid IN (
    SELECT ctid FROM oauth_identities WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'oauth_identities'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM user_clusters WHERE ctid IN (
    SELECT ctid FROM user_clusters WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'user_clusters'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM user_cvs WHERE ctid IN (
    SELECT ctid FROM user_cvs WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'user_cvs'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  UPDATE api_usage SET user_id = NULL WHERE ctid IN (
    SELECT ctid FROM api_usage WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'api_usage:anonimizzate'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM users WHERE id = p_user_id;
  GET DIAGNOSTICS n = ROW_COUNT;
  step := 'users'; rows_affected := n; RETURN NEXT;
  RETURN;
END $$;


-- Potatura di token e sessioni scaduti. Da mettere in cron insieme alla
-- retention delle offerte: senza, login_tokens cresce all'infinito e con esso
-- lo storico degli indirizzi IP, che è un dato personale che non ci serve.
CREATE OR REPLACE FUNCTION purge_expired_auth(p_grace_days integer DEFAULT 7)
RETURNS TABLE (step text, rows_affected bigint)
LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
  v_cutoff timestamptz := now() - make_interval(days => p_grace_days);
BEGIN
  DELETE FROM login_tokens WHERE expires_at < v_cutoff;
  GET DIAGNOSTICS n = ROW_COUNT;
  step := 'login_tokens'; rows_affected := n; RETURN NEXT;

  DELETE FROM sessions WHERE expires_at < v_cutoff OR revoked_at < v_cutoff;
  GET DIAGNOSTICS n = ROW_COUNT;
  step := 'sessions'; rows_affected := n; RETURN NEXT;
  RETURN;
END $$;
