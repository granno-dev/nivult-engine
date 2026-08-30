-- I CV superati smettono di conservare per sempre cio' che abbiamo letto.
--
-- Caricando un CV nuovo, il file cifrato del precedente viene gia'
-- eliminato dallo storage. La RIGA pero' resta, e con lei `raw_extraction`:
-- headline, storia dei ruoli con i datori, formazione, certificazioni. La
-- ragione per tenere la riga e' buona — dice quale versione del profilo ha
-- prodotto un dato match, ed e' meta' della tracciabilita' che l'AI Act
-- chiede — ma non e' una ragione per tenerne il CONTENUTO oltre il momento
-- in cui serve a qualcosa.
--
-- Serve sapere QUANDO un CV e' stato superato: senza, il periodo di grazia
-- non ha da dove partire. `uploaded_at` non va bene, e' la data opposta.
ALTER TABLE user_cvs
  ADD COLUMN IF NOT EXISTS superseded_at timestamptz;

COMMENT ON COLUMN user_cvs.superseded_at IS
  'Quando questo CV e'' stato sostituito da uno piu'' recente. NULL per il '
  'CV attivo. Da qui parte il periodo di grazia prima dello svuotamento '
  'dei dati estratti.';

-- Le righe gia' superate non hanno una data e non c'e' modo di ricavarla:
-- si fa partire l'orologio da adesso. E' la scelta prudente — allunga la
-- grazia invece di accorciarla — e va detta, perche' altrimenti fra un
-- mese sembrera' che quei CV siano stati superati tutti lo stesso giorno.
UPDATE user_cvs SET superseded_at = now()
 WHERE status = 'superseded' AND superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS user_cvs_superseded_idx
  ON user_cvs (superseded_at)
  WHERE status = 'superseded' AND raw_extraction IS NOT NULL;
