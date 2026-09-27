-- 0065 — I reveal del portale si ricordano (27/09/2026).
--
-- Il modello visto su TheirStack: rivelare un'azienda o un'offerta costa
-- un credito, MA la rivelazione resta tua — tornarci sopra domani non
-- paga di nuovo. Una riga per (utente, tipo, riferimento): la chiave
-- primaria e' la deduplica.

CREATE TABLE portale_rivelazioni (
  user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  tipo         text        NOT NULL CHECK (tipo IN ('job', 'azienda')),
  riferimento  text        NOT NULL,
  revealed_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, tipo, riferimento)
);
