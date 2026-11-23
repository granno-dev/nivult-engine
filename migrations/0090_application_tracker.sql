-- 0090 — Tracker personale, colloqui, documenti cifrati e deduplica push.

ALTER TABLE matches
  ADD COLUMN note text,
  ADD COLUMN contact text,
  ADD COLUMN next_action text,
  ADD COLUMN next_action_at timestamptz;

ALTER TABLE matches ADD CONSTRAINT matches_note_length_ck
  CHECK (note IS NULL OR char_length(note) <= 10000);
ALTER TABLE matches ADD CONSTRAINT matches_contact_length_ck
  CHECK (contact IS NULL OR char_length(contact) BETWEEN 1 AND 500);
ALTER TABLE matches ADD CONSTRAINT matches_next_action_ck CHECK (
  (next_action IS NULL OR char_length(next_action) BETWEEN 1 AND 500)
  AND (next_action_at IS NULL OR next_action IS NOT NULL)
);

ALTER TABLE matches DROP CONSTRAINT matches_dismiss_reason_ck;
ALTER TABLE matches ADD CONSTRAINT matches_dismiss_reason_ck
  CHECK (dismiss_reason IS NULL OR dismiss_reason IN (
    'company','target','salary','location','seniority','work_arrangement','skills'));

CREATE TABLE match_interviews (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  match_id         uuid        NOT NULL,
  user_id          uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  starts_at        timestamptz NOT NULL,
  note             text,
  reminder_minutes integer,
  created_at       timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT match_interviews_match_fk FOREIGN KEY (match_id, user_id)
    REFERENCES matches (id, user_id) ON DELETE CASCADE,
  CONSTRAINT match_interviews_note_ck
    CHECK (note IS NULL OR char_length(note) <= 10000),
  CONSTRAINT match_interviews_reminder_ck
    CHECK (reminder_minutes IS NULL OR reminder_minutes BETWEEN 0 AND 43200)
);

CREATE INDEX match_interviews_match_idx
  ON match_interviews (match_id, user_id, starts_at, id);
CREATE INDEX match_interviews_reminder_idx
  ON match_interviews (user_id, starts_at, id)
  WHERE reminder_minutes IS NOT NULL;

CREATE TABLE match_documents (
  id                uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  match_id          uuid        NOT NULL,
  user_id           uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  original_filename text        NOT NULL,
  mime_type         text        NOT NULL,
  size_bytes        bigint      NOT NULL,
  sha256            char(64)    NOT NULL,
  storage_key       text        NOT NULL UNIQUE,
  encryption_algo   text        NOT NULL,
  encrypted_dek     bytea       NOT NULL,
  nonce             bytea       NOT NULL,
  auth_tag          bytea       NOT NULL,
  kek_version       smallint    NOT NULL,
  created_at        timestamptz NOT NULL DEFAULT now(),

  CONSTRAINT match_documents_match_fk FOREIGN KEY (match_id, user_id)
    REFERENCES matches (id, user_id) ON DELETE CASCADE,
  CONSTRAINT match_documents_filename_ck
    CHECK (char_length(original_filename) BETWEEN 1 AND 255),
  CONSTRAINT match_documents_mime_ck CHECK (mime_type IN (
    'application/pdf',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'text/plain')),
  CONSTRAINT match_documents_size_ck
    CHECK (size_bytes > 0 AND size_bytes <= 10485760),
  CONSTRAINT match_documents_sha256_ck CHECK (sha256 ~ '^[0-9a-f]{64}$'),
  CONSTRAINT match_documents_encryption_ck CHECK (
        encryption_algo = 'aes-256-gcm'
    AND length(encrypted_dek) >= 32
    AND length(nonce) = 12
    AND length(auth_tag) = 16
    AND kek_version > 0
  )
);

CREATE INDEX match_documents_match_idx
  ON match_documents (match_id, user_id, created_at, id);
CREATE INDEX match_documents_user_idx
  ON match_documents (user_id, created_at, id);

CREATE TABLE personal_notification_dedup (
  user_id    uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind       text        NOT NULL,
  event_key  text        NOT NULL,
  sent_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, kind, event_key),
  CONSTRAINT personal_notification_kind_ck CHECK (kind IN (
    'interview','next_action','deadline','closed','weekly'))
);

CREATE INDEX personal_notification_sent_idx
  ON personal_notification_dedup (sent_at);

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

  DELETE FROM match_documents WHERE ctid IN (
    SELECT ctid FROM match_documents WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'match_documents'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM match_interviews WHERE ctid IN (
    SELECT ctid FROM match_interviews WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'match_interviews'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM matches WHERE ctid IN (
    SELECT ctid FROM matches WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'matches'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM personal_notification_dedup WHERE ctid IN (
    SELECT ctid FROM personal_notification_dedup
    WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'personal_notification_dedup'; rows_affected := n; RETURN NEXT; RETURN; END IF;

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
