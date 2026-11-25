-- 0087 — Frequenza del digest per singola ricerca.
--
-- NULL sulla frequenza significa eredita' dall'account: niente backfill e
-- nessuna scadenza specifica inventata per le righe esistenti. Le date di
-- consegna restano sulla stessa riga della ricerca e seguono la cascata GDPR.

ALTER TABLE user_clusters
  ADD COLUMN frequency text,
  ADD COLUMN send_weekday smallint,
  ADD COLUMN send_monthday smallint,
  ADD COLUMN send_hour_local smallint,
  ADD COLUMN timezone text,
  ADD COLUMN next_digest_at timestamptz,
  ADD COLUMN last_digest_at timestamptz;

ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_frequency_ck
  CHECK (frequency IS NULL OR frequency IN ('daily', 'weekly', 'monthly'));

ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_send_weekday_ck
  CHECK (send_weekday IS NULL OR send_weekday BETWEEN 1 AND 7);
ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_send_monthday_ck
  CHECK (send_monthday IS NULL OR send_monthday BETWEEN 1 AND 28);
ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_send_hour_ck
  CHECK (send_hour_local IS NULL OR send_hour_local BETWEEN 0 AND 23);

ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_schedule_ck CHECK (
     (frequency IS NULL AND send_weekday IS NULL AND send_monthday IS NULL
      AND send_hour_local IS NULL AND timezone IS NULL AND next_digest_at IS NULL)
  OR (frequency IS NOT NULL AND (
       (frequency = 'daily' AND send_weekday IS NULL AND send_monthday IS NULL)
    OR (frequency = 'weekly' AND send_weekday IS NOT NULL AND send_monthday IS NULL)
    OR (frequency = 'monthly' AND send_weekday IS NULL AND send_monthday IS NOT NULL)
  ))
);

CREATE TRIGGER user_clusters_valid_timezone
  BEFORE INSERT OR UPDATE OF timezone ON user_clusters
  FOR EACH ROW EXECUTE FUNCTION assert_valid_timezone();

CREATE INDEX user_clusters_due_idx ON user_clusters (user_id, next_digest_at)
  WHERE frequency IS NOT NULL AND next_digest_at IS NOT NULL;

COMMENT ON COLUMN user_clusters.frequency IS
  'NULL = eredita la pianificazione dell''account; altrimenti daily, weekly o monthly.';
COMMENT ON COLUMN user_clusters.send_hour_local IS
  'Ora specifica della ricerca; NULL = ora dell''account.';
COMMENT ON COLUMN user_clusters.timezone IS
  'Fuso specifico della ricerca; NULL = fuso dell''account.';
COMMENT ON COLUMN user_clusters.next_digest_at IS
  'Prossimo slot dell''override; users.next_digest_at resta lo slot delle ricerche ereditate.';
COMMENT ON COLUMN user_clusters.last_digest_at IS
  'Ultimo slot con almeno un elemento realmente incluso per questa ricerca.';
