-- 0070 — Lo storico degli acquisti di crediti (04/10/2026).
--
-- creem_eventi ricorda solo l'id del webhook (idempotenza); chi paga
-- vuole vedere QUANDO ha comprato, QUANTO e a che prezzo. Una riga per
-- accredito, scritta da billing.applica_pagamento nello stesso giro
-- dell'UPDATE su users.crediti_extra.

CREATE TABLE portale_acquisti (
  id          bigserial   PRIMARY KEY,
  user_id     uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  at          timestamptz NOT NULL DEFAULT now(),
  crediti     int         NOT NULL,
  -- l'importo come lo manda Creem (centesimi + valuta); puo' mancare
  importo_cent int,
  valuta       text,
  evento_id   text        NOT NULL UNIQUE  -- un webhook = una riga, mai due
);
CREATE INDEX portale_acquisti_utente_idx ON portale_acquisti (user_id, at DESC);
GRANT SELECT, INSERT ON portale_acquisti TO nivult_app;
GRANT USAGE, SELECT ON SEQUENCE portale_acquisti_id_seq TO nivult_app;
