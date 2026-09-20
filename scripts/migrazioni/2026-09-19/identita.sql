-- L'identita' dell'azienda (19/09/2026): nome, bacheca o datore, doppioni.
--
-- Tre difetti che hanno la stessa natura, e che su un prodotto venduto per
-- azienda pesano piu' di un dominio in piu':
--   - 8.090 righe senza nome (12,3%): un'azienda senza nome non si vende;
--   - tenant che sono BACHECHE, non datori: mindpal.co dichiara 769 nomi di
--     aziende diverse nelle sue offerte. Dargli un nome vorrebbe dire dare a una
--     bacheca il nome di un suo cliente;
--   - ~1.228 righe che sono la stessa azienda vista da piu' tenant.

-- da dove viene il nome: 'ats' se l'ha dato la piattaforma, 'dichiarato' se
-- l'abbiamo letto dal JSON-LD dell'annuncio. Mai una nostra invenzione senza dirlo.
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS company_name_source text;

ALTER TABLE aziende_vendibili ADD COLUMN IF NOT EXISTS nome_fonte       text;
-- quanti nomi di datori diversi compaiono nelle sue offerte: 1 = azienda,
-- centinaia = bacheca. E' la prova che distingue i due casi.
ALTER TABLE aziende_vendibili ADD COLUMN IF NOT EXISTS nomi_dichiarati  int;
ALTER TABLE aziende_vendibili ADD COLUMN IF NOT EXISTS e_bacheca        boolean NOT NULL DEFAULT false;
-- il capofila del gruppo: piu' tenant della stessa azienda puntano alla stessa
-- riga. Non si cancella niente — si dice solo chi rappresenta chi.
ALTER TABLE aziende_vendibili ADD COLUMN IF NOT EXISTS canonico_id      uuid;

CREATE INDEX IF NOT EXISTS aziende_vendibili_canonico_idx ON aziende_vendibili (canonico_id);
CREATE INDEX IF NOT EXISTS aziende_vendibili_bacheca_idx  ON aziende_vendibili (e_bacheca) WHERE e_bacheca;

-- La vista che si vende davvero: un'azienda per riga, niente bacheche, un nome.
CREATE OR REPLACE VIEW aziende_pronte AS
SELECT * FROM aziende_vendibili
 WHERE NOT e_bacheca
   AND nome IS NOT NULL
   AND (canonico_id IS NULL OR canonico_id = company_id);

GRANT SELECT ON aziende_pronte TO nivult_operaio, nivult_app;
