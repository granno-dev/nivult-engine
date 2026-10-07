-- 0071: la coda della chiave API, per riconoscerla nella lista del portale
--
-- Nata il 07/10/2026: il portale mostrava «•••383970» prendendo le ultime
-- 6 dell'UUID di riga — che con la chiave in chiaro appena mostrata nel
-- banner non c'entra nulla, e l'utente nuovo vedeva due "chiavi" diverse.
-- La chiave in chiaro non la conserviamo (solo l'hash), quindi la coda
-- va salvata alla creazione. Le chiavi esistenti restano con coda NULL:
-- il portale non mostra fingerprint per quelle.

ALTER TABLE api_chiavi ADD COLUMN IF NOT EXISTS key_tail text;

ALTER TABLE api_chiavi DROP CONSTRAINT IF EXISTS api_chiavi_key_tail_check;
ALTER TABLE api_chiavi ADD CONSTRAINT api_chiavi_key_tail_check
  CHECK (key_tail IS NULL OR key_tail ~ '^[A-Za-z0-9_-]{6}$');
