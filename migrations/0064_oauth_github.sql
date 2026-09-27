-- 0064 — GitHub entra nei provider OAuth ammessi (il portale B2B).
--
-- Stessa cura della 0059/0060 per LinkedIn: quattro vincoli CHECK tengono
-- la lista chiusa — provider sui giri e sulle identita', origin sui
-- gettoni e sulle sessioni. Se ne resta chiuso anche uno solo, il login
-- GitHub muore a meta' giro.
ALTER TABLE oauth_flows DROP CONSTRAINT IF EXISTS oauth_flows_provider_check;
ALTER TABLE oauth_flows ADD CONSTRAINT oauth_flows_provider_check
  CHECK (provider = ANY (ARRAY['google','microsoft','linkedin','github']));

ALTER TABLE oauth_identities DROP CONSTRAINT IF EXISTS oauth_identities_provider_check;
ALTER TABLE oauth_identities ADD CONSTRAINT oauth_identities_provider_check
  CHECK (provider = ANY (ARRAY['google','microsoft','linkedin','github']));

ALTER TABLE login_tokens DROP CONSTRAINT IF EXISTS login_tokens_origin_check;
ALTER TABLE login_tokens ADD CONSTRAINT login_tokens_origin_check
  CHECK (origin = ANY (ARRAY['magic_link','google','microsoft','linkedin','github']));

ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_origin_check;
ALTER TABLE sessions ADD CONSTRAINT sessions_origin_check
  CHECK (origin = ANY (ARRAY['magic_link','google','microsoft','linkedin','github']));

-- dove atterrare a fine giro (27/09/2026): il portale B2B e' un'altra
-- pagina dello stesso sito, non /verify del digest. NULL = comportamento
-- di sempre, nessun giro vecchio cambia rotta.
ALTER TABLE oauth_flows ADD COLUMN IF NOT EXISTS destinazione text;
