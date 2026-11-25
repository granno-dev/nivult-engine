-- 0076: i token push Expo dell'app nativa (Mavult), uno per dispositivo.
--
-- L'app registra il token dopo l'accesso; il worker dei digest lo usa per
-- avvisare che il digest del giorno e' pronto. Il token e' quasi un dato
-- personale — attraverso Expo identifica il dispositivo, e con il
-- dispositivo la persona — quindi niente storico: una riga per coppia
-- (utente, token), rinfrescata a ogni registrazione. E' Expo stessa a
-- dirci quando un token muore (DeviceNotRegistered), e la riga se ne va
-- con lui; se muore l'utente, il CASCADE pensa al resto.
--
-- I permessi non si toccano: la 0010 gia' concede DML su ogni tabella
-- creata dal migratore (ALTER DEFAULT PRIVILEGES), e vale tanto per
-- nivult_app quanto per mavult_app.
CREATE TABLE user_push_tokens (
  user_id    uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token      text        NOT NULL,
  platform   text        NOT NULL CHECK (platform IN ('ios','android')),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, token)
);

-- La pulizia dei token morti parte dal token (DeviceNotRegistered), non
-- dall'utente: senza questo indice ogni risposta di Expo farebbe una scansione.
CREATE INDEX user_push_tokens_token_idx ON user_push_tokens (token);

COMMENT ON TABLE user_push_tokens IS
  'Token push Expo dei dispositivi dell''app nativa: li scrive l''app via API, li legge (e li pota) il worker dei digest.';
