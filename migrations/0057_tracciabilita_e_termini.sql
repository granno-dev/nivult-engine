-- Due obblighi diversi, una migrazione sola perche' sono due colonne.
--
-- 1. TRACCIABILITA' DELLA DECISIONE (AI Act, sistemi ad alto rischio)
--
-- `matches` registrava gia' punteggio, motivo, soglia, modello e quale CV
-- e' stato usato: e' molto piu' della media. Mancava la cosa che rende una
-- decisione RIPRODUCIBILE: quale rubrica l'ha prodotta. Il 30/08/2026 la
-- rubrica e' cambiata due volte in un giorno (soglia a 70, regole su paese
-- e famiglia), e senza questa colonna i match di ieri e quelli di oggi
-- sono numeri sulla stessa scala che significano cose diverse.
--
-- Serve a noi prima che a un'autorita': e' la colonna che permette di dire
-- «questi 400 match vengono dalla rubrica che sottovalutava i passaggi
-- laterali» invece di rifare tutto a mano.
ALTER TABLE matches
  ADD COLUMN IF NOT EXISTS rubric_version text;

COMMENT ON COLUMN matches.rubric_version IS
  'La versione della rubrica che ha prodotto questo punteggio. Si alza a '
  'mano in matching/llm.py quando la rubrica cambia in modo che sposta i '
  'punteggi. NULL = valutato prima del 30/08/2026.';

CREATE INDEX IF NOT EXISTS matches_rubric_version_idx
  ON matches (rubric_version, evaluated_at DESC);

-- 2. PROVA DELL'ACCETTAZIONE (GDPR art. 7(1) per il consenso, art. 6(1)(b)
--    per il contratto)
--
-- La base giuridica del trattamento del CV e' il CONTRATTO, non il
-- consenso: la privacy policy lo dice, ed e' la scelta giusta per un
-- servizio dove senza CV non c'e' prodotto. Ma di un contratto bisogna
-- poter mostrare che l'altra parte lo ha accettato, e quando, e in quale
-- versione. Oggi l'iscrizione e' un solo campo email e non resta traccia
-- di niente.
ALTER TABLE users
  ADD COLUMN IF NOT EXISTS terms_accepted_at timestamptz,
  ADD COLUMN IF NOT EXISTS terms_version text;

COMMENT ON COLUMN users.terms_version IS
  'La versione dei documenti accettati (data di revisione, es. 2026-08-28). '
  'Cambiare i termini e non alzare questa stringa vuol dire non sapere piu'' '
  'chi ha accettato cosa.';
