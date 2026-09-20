-- La tabella che si vende (19/09/2026).
--
-- Il mercato non compra annunci, compra AZIENDE: TheirStack fa pagare un credito
-- per ogni azienda rivelata, tre per una azienda restituita dall'API o per una
-- ricerca di technographics. L'annuncio e' la materia prima; il prodotto e'
-- l'azienda, con un dominio a cui agganciarla e le tecnologie che usa.
--
-- E' una TABELLA, non una vista: aggregare le tecnologie di 2,6 milioni di offerte
-- a ogni interrogazione costerebbe minuti. Si ricostruisce con costruisci_aziende.py.
CREATE TABLE IF NOT EXISTS aziende_vendibili (
  company_id     uuid PRIMARY KEY,
  nome           text,
  piattaforma    text,
  slug           text,
  -- l'aggancio al mondo reale: senza dominio l'azienda non vale niente
  dominio        text,
  dominio_fonte  text,
  -- cio' che sappiamo di lei
  paese          text,
  settore        text,
  dipendenti     text,
  lei            text,
  -- cio' che dicono le sue offerte
  offerte_attive int  NOT NULL DEFAULT 0,
  ultima_offerta timestamptz,
  offerte_lette  int  NOT NULL DEFAULT 0,   -- quante il 2B ha davvero letto
  tecnologie     jsonb,                     -- [{nome, offerte}] dalla piu' citata
  n_tecnologie   int  NOT NULL DEFAULT 0,
  aggiornato_at  timestamptz NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS aziende_vendibili_dominio_idx  ON aziende_vendibili (dominio)  WHERE dominio IS NOT NULL;
CREATE INDEX IF NOT EXISTS aziende_vendibili_paese_idx    ON aziende_vendibili (paese);
CREATE INDEX IF NOT EXISTS aziende_vendibili_settore_idx  ON aziende_vendibili (settore);
CREATE INDEX IF NOT EXISTS aziende_vendibili_tec_idx      ON aziende_vendibili USING gin (tecnologie);

ALTER TABLE aziende_vendibili OWNER TO nivult;
GRANT SELECT, INSERT, UPDATE, DELETE ON aziende_vendibili TO nivult_operaio;
GRANT SELECT ON aziende_vendibili TO nivult_lettore;
