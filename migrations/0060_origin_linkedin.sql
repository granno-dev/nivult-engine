-- 'linkedin' fra le origini ammesse di login_tokens e sessions.
--
-- La 0059 aveva esteso i vincoli sui PROVIDER (oauth_flows,
-- oauth_identities) ma non quelli sull'ORIGINE: due colonne diverse con lo
-- stesso difetto, e la seconda meta' e' saltata fuori solo quando il primo
-- login LinkedIn e' arrivato in fondo. `concludi` scrive origin='linkedin'
-- in login_tokens, e /verify la ricopia in sessions quando il gettone
-- diventa una sessione: entrambe rifiutavano il valore.
ALTER TABLE login_tokens DROP CONSTRAINT IF EXISTS login_tokens_origin_check;
ALTER TABLE login_tokens ADD CONSTRAINT login_tokens_origin_check
  CHECK (origin = ANY (ARRAY['magic_link','google','microsoft','linkedin']));

ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_origin_check;
ALTER TABLE sessions ADD CONSTRAINT sessions_origin_check
  CHECK (origin = ANY (ARRAY['magic_link','google','microsoft','linkedin']));
