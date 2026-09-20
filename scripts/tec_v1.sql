-- La testa tecnologie di v1 scrive qui, non in estrazioni_v2b.
--
-- estrazioni_v2b ha job_id come chiave primaria: una riga per annuncio. Scrivere
-- li' cancellerebbe l'uscita del 2B, e per settimane vorremo confrontare i due
-- sullo stesso annuncio — e poter tornare indietro se la testa sbaglia su
-- qualcosa che il golden non copriva.
--
-- Le tecnologie sono stringhe PRESE DAL TESTO (la testa marca, non riscrive):
-- «excel» se l'annuncio scrive «excel». La normalizzazione in forma commerciale
-- sta a valle, nella tabella degli alias, e si puo' cambiare senza rifare
-- l'estrazione.

CREATE TABLE IF NOT EXISTS tecnologie_v1 (
  job_id      uuid        PRIMARY KEY,
  tecnologie  jsonb       NOT NULL,          -- ["excel", "sap", ...] come nel testo
  quante      int         NOT NULL,          -- ridondante ma comodo per le viste
  modello     text        NOT NULL,          -- es. 'tec-v1-ck02500'
  soglia      real        NOT NULL,          -- la manopola usata: serve a rifare il conto
  testo_hash  text,                          -- per le gemelle: stesso testo, stessa risposta
  creato_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tecnologie_v1_creato ON tecnologie_v1 (creato_at);
CREATE INDEX IF NOT EXISTS tecnologie_v1_hash   ON tecnologie_v1 (testo_hash);
-- per cercare le aziende che usano una tecnologia: e' la query del prodotto
CREATE INDEX IF NOT EXISTS tecnologie_v1_gin    ON tecnologie_v1 USING gin (tecnologie);

-- Il marcatore della coda, come locale_v1_at per v1 e estratto_2b_at per il 2B.
-- Due colonne: quando e' stato fatto, e quando e' stato PRENOTATO (la prenotazione
-- scade, cosi' se un operaio muore a meta' le righe tornano libere da sole).
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS tec_v1_at   timestamptz;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS preso_tec_at timestamptz;

-- L'indice parziale che rende istantanea la ricerca della coda. Senza, la query
-- di prenotazione legge l'intera tabella: e' il guasto del 16/09 che teneva il
-- demone del 2B fermo per minuti a ogni giro.
CREATE INDEX IF NOT EXISTS ats_jobs_tec_v1_coda
    ON ats_jobs (posted_at DESC NULLS LAST)
 WHERE expired_at IS NULL AND tec_v1_at IS NULL;
