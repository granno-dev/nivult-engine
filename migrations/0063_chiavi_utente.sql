-- 0063 — Le chiavi B2B appartengono a un utente del portale.
--
-- Prima le chiavi si creavano solo da CLI e vivevano sole: una label in
-- parole nostre e basta. Con il portale self-service ogni chiave creata
-- da un cliente porta il suo user_id; le chiavi interne di sempre (le
-- nostre, quelle di prova) restano con user_id NULL e invisibili agli
-- endpoint del portale.
--
-- La scelta della COLONNA e non di una tabella ponte: una chiave ha un
-- solo proprietario, sempre. Il lock resta basso perche' l'ALTER e'
-- metadata-only (colonna nullable senza default).

ALTER TABLE api_chiavi ADD COLUMN IF NOT EXISTS user_id uuid
  REFERENCES users(id) ON DELETE SET NULL;
-- la ricerca per utente e' il percorso caldo del portale
CREATE INDEX IF NOT EXISTS api_chiavi_user_idx ON api_chiavi (user_id)
  WHERE user_id IS NOT NULL;
