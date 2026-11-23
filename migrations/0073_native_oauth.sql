-- 0073: PKCE dell'app nativa, distinto dal PKCE server-to-server del provider.
--
-- La sfida pubblica resta sul flusso per tornare al callback e viene copiata
-- sul gettone monouso: /auth/consuma puo' cosi' pretendere il verifier senza
-- cambiare il consumo dei gettoni web, che hanno native_challenge NULL.
ALTER TABLE oauth_flows ADD COLUMN native_state text;
ALTER TABLE oauth_flows ADD COLUMN native_challenge text;
ALTER TABLE login_tokens ADD COLUMN native_challenge text;

ALTER TABLE oauth_flows ADD CONSTRAINT oauth_flows_native_ck CHECK (
  (
    destinazione = 'nivult://oauth/callback'
    AND native_state IS NOT NULL
    AND length(native_state) BETWEEN 1 AND 512
    AND native_challenge IS NOT NULL
    AND native_challenge ~ '^[A-Za-z0-9_-]{43}$'
  )
  OR
  (
    destinazione IS DISTINCT FROM 'nivult://oauth/callback'
    AND native_state IS NULL
    AND native_challenge IS NULL
  )
);

ALTER TABLE login_tokens ADD CONSTRAINT login_tokens_native_challenge_ck
  CHECK (native_challenge IS NULL OR native_challenge ~ '^[A-Za-z0-9_-]{43}$');

COMMENT ON COLUMN oauth_flows.native_state IS
  'State dell''app nativa, separato dallo state OAuth del provider.';
COMMENT ON COLUMN oauth_flows.native_challenge IS
  'Sfida S256 dell''app nativa; il verifier non viene mai conservato.';
COMMENT ON COLUMN login_tokens.native_challenge IS
  'Sfida S256 richiesta per consumare questo gettone nativo; NULL per i flussi web.';
