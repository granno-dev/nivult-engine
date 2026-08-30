-- LinkedIn si aggiunge ai provider OAuth ammessi.
--
-- Due vincoli CHECK tenevano la lista a google/microsoft: uno sui giri in
-- corso (oauth_flows), uno sulle identita' collegate (oauth_identities).
-- Vanno allargati entrambi, o l'INSERT del giro fallisce prima ancora di
-- parlare con LinkedIn.
--
-- Il verifier PKCE resta obbligatorio in tabella (minimo 43 caratteri): per
-- questo il codice continua a generarlo e a salvarlo anche per LinkedIn,
-- che PKCE non lo usa — lo storo, non lo spedisco. Cambiare quel vincolo
-- per accettare NULL sarebbe piu' lavoro e meno sicuro del tenere una
-- stringa casuale inutile.
ALTER TABLE oauth_flows DROP CONSTRAINT IF EXISTS oauth_flows_provider_check;
ALTER TABLE oauth_flows ADD CONSTRAINT oauth_flows_provider_check
  CHECK (provider = ANY (ARRAY['google','microsoft','linkedin']));

ALTER TABLE oauth_identities DROP CONSTRAINT IF EXISTS oauth_identities_provider_check;
ALTER TABLE oauth_identities ADD CONSTRAINT oauth_identities_provider_check
  CHECK (provider = ANY (ARRAY['google','microsoft','linkedin']));
