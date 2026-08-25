-- 0042 — Come si chiama l'utente.
--
-- Il pannello lo salutava con l'indirizzo email. E' l'unica cosa che
-- avevamo: `users` non ha mai avuto un nome, perche' il magic link chiede
-- solo un indirizzo e non serviva altro per far funzionare il motore.
--
-- Ma Google e Microsoft ce lo mandano gia' dentro l'id_token — `name`,
-- `given_name`, `family_name` — e finora lo buttavamo via leggendo solo
-- `sub` ed `email`. Prenderlo non costa una chiamata in piu' ne' un
-- consenso in piu': e' nello stesso scope `profile` che l'utente ha gia'
-- approvato.
ALTER TABLE users ADD COLUMN display_name text
  CHECK (display_name IS NULL OR length(display_name) BETWEEN 1 AND 120);

COMMENT ON COLUMN users.display_name IS
  'Nome e cognome, da OAuth quando c''e'' o scritto dall''utente. Mai '
  'obbligatorio: con il magic link non lo si conosce, e le iniziali '
  'ricavate dall''indirizzo bastano a non salutare nessuno con una email.';
