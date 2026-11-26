-- 0099 — Quote Explorer condivise fra worker e reveal company a 3 crediti.
-- I bucket sono aggiornati con UPSERT atomico; il giorno e' UTC.

CREATE TABLE explorer_rate_limits (
  scope        text        NOT NULL CHECK (scope IN ('user_minute', 'ip_minute', 'user_day')),
  subject      text        NOT NULL,
  window_start timestamptz NOT NULL,
  hits         integer     NOT NULL CHECK (hits >= 0),
  PRIMARY KEY (scope, subject, window_start)
);

CREATE INDEX explorer_rate_limits_window_idx
  ON explorer_rate_limits (window_start);

GRANT SELECT, INSERT, UPDATE, DELETE ON explorer_rate_limits TO nivult_app;
