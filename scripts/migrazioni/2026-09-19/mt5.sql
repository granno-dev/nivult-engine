-- Struttura per il demone mT5 e per la coda di ripasso del 2B (19/09/2026).
-- Tutto additivo: niente tocca quello che gira adesso.

ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS sintesi_mt5_at timestamptz;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS preso_mt5_at   timestamptz;

CREATE TABLE IF NOT EXISTS sintesi_mt5 (
  job_id            uuid PRIMARY KEY,
  sintesi           text,
  fiducia           real,          -- media del log-prob: quanto il modello ci credeva
  modello           text NOT NULL,
  testo_hash        text,
  ripassata_at      timestamptz,   -- quando il 2B l'ha riscritta
  presa_ripasso_at  timestamptz,
  creato_at         timestamptz NOT NULL DEFAULT now());

ALTER TABLE sintesi_mt5 OWNER TO nivult;
GRANT SELECT, INSERT, UPDATE, DELETE ON sintesi_mt5 TO nivult_operaio;
