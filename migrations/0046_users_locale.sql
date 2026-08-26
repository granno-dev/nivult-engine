-- 0046 — La lingua dell'utente, finalmente sulla riga dell'utente.
--
-- Il sito parla nove lingue; il motore finora nessuna: il template del
-- digest era in italiano fisso, il magic link pure, e la rubrica di GLM
-- IMPONEVA la motivazione in italiano — per tutti, tedeschi compresi.
--
-- `user_cvs.languages` non c'entra niente: quelle sono le lingue che il
-- CANDIDATO parla, estratte dal CV. Questa è la lingua in cui l'utente
-- legge, e va letta in due posti: il modello dell'email e il prompt di GLM.
--
-- Il default è 'en', non NULL: un'email deve sempre poter partire, e
-- l'inglese è il ripiego meno sbagliato. Il valore vero arriva dal sito —
-- alla creazione dell'utente (la lingua della pagina da cui ha chiesto il
-- link) e a ogni salvataggio delle preferenze.
--
-- CHECK e non tabella di vocabolario: aggiungere una lingua al sito è già
-- un rilascio del sito, e la migrazione che allarga questo CHECK viaggia
-- naturalmente insieme. Una tabella sarebbe cerimonia per nove righe.
ALTER TABLE users
  ADD COLUMN locale text NOT NULL DEFAULT 'en'
  CHECK (locale IN ('en','it','fr','de','es','pt','nl','pl','sv'));

COMMENT ON COLUMN users.locale IS
  'La lingua in cui l''utente legge: email del digest, magic link e '
  'motivazioni GLM. Non e'' user_cvs.languages, che sono le lingue che il '
  'candidato parla.';
