-- 0086 — Feedback, archivio personale e tracker manuale sul match esistente.
--
-- L'archivio utente non e' la lapide tecnica jobs.purged_at. La versione serve
-- alla concorrenza ottimistica; i timestamp di prima transizione restano sul
-- match anche quando la ricerca o l'offerta escono dalla discovery.
BEGIN;

ALTER TABLE matches
  ADD COLUMN saved_at timestamptz,
  ADD COLUMN dismissed_at timestamptz,
  ADD COLUMN dismiss_reason text,
  ADD COLUMN archived_at timestamptz,
  ADD COLUMN application_status text NOT NULL DEFAULT 'not_started',
  ADD COLUMN applied_at timestamptz,
  ADD COLUMN action_version bigint NOT NULL DEFAULT 0,
  ADD COLUMN user_action_at timestamptz;

ALTER TABLE matches ADD CONSTRAINT matches_application_status_ck
  CHECK (application_status IN ('not_started','applied','interview','offer','rejected'));

ALTER TABLE matches ADD CONSTRAINT matches_dismiss_reason_ck
  CHECK (dismiss_reason IS NULL OR dismiss_reason IN ('company','target'));

ALTER TABLE matches ADD CONSTRAINT matches_dismiss_state_ck CHECK (
  (dismissed_at IS NULL) = (dismiss_reason IS NULL)
  AND (dismissed_at IS NULL OR
       (saved_at IS NULL AND application_status = 'not_started'))
);

ALTER TABLE matches ADD CONSTRAINT matches_applied_at_ck CHECK (
  (application_status = 'not_started' AND applied_at IS NULL)
  OR (application_status <> 'not_started' AND applied_at IS NOT NULL)
);

ALTER TABLE matches ADD CONSTRAINT matches_action_version_ck
  CHECK (action_version >= 0);

ALTER TABLE matches ADD CONSTRAINT matches_user_action_ck CHECK (
  (action_version = 0 AND user_action_at IS NULL
   AND saved_at IS NULL AND dismissed_at IS NULL AND archived_at IS NULL
   AND application_status = 'not_started')
  OR
  (action_version > 0 AND user_action_at IS NOT NULL
   AND (saved_at IS NULL OR saved_at <= user_action_at)
   AND (dismissed_at IS NULL OR dismissed_at <= user_action_at)
   AND (archived_at IS NULL OR archived_at <= user_action_at)
   AND (applied_at IS NULL OR applied_at <= user_action_at))
);

CREATE INDEX matches_user_actions_idx
  ON matches (user_id, user_action_at DESC)
  WHERE user_action_at IS NOT NULL;

ALTER TABLE jobs ADD COLUMN date_posted_declared boolean;

COMMENT ON COLUMN jobs.date_posted_declared IS
  'TRUE solo se date_posted e'' dichiarata dalla fonte; FALSE per stime/fallback; NULL sulle righe senza prova.';

COMMIT;
