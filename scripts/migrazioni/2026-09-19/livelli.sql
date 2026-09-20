-- Quanto e' solido un dominio: il livello si dichiara, non si nasconde (19/09/2026).
--
--  1  il sito rimanda al NOSTRO tenant ATS. E' una prova, non una somiglianza.
--     E' l'unica che prende i casi che il nome non direbbe mai: Orbotech ->
--     kla.com (acquisita), WSH Group -> caterlinkltd.co.uk (controllata).
--  2  il dominio CORRISPONDE al nome dell'azienda (somiglianza >= 0,80 dopo aver
--     tolto le forme societarie). Calibrato su 1.003 coppie gia' provate dal
--     giudice: ci sta dentro l'83,5% di quelle vere e lo 0,2% di coppie
--     accoppiate a caso — un falso ogni cinquecento.
--  3  primo risultato di un motore di ricerca, senza altra conferma. Debole.
--
-- Scaricare il sito per verificarlo NON funziona e l'ho misurato: Cloudflare
-- risponde 403 e le testate da browser non cambiano niente.
ALTER TABLE ats_companies      ADD COLUMN IF NOT EXISTS site_domain_livello smallint;
ALTER TABLE aziende_vendibili  ADD COLUMN IF NOT EXISTS dominio_livello     smallint;

-- i domini che abbiamo gia' sono tutti passati dal giudice: sono livello 1
UPDATE ats_companies
   SET site_domain_livello = 1
 WHERE site_domain IS NOT NULL AND site_domain_livello IS NULL;

CREATE INDEX IF NOT EXISTS ats_companies_livello_idx
  ON ats_companies (site_domain_livello) WHERE site_domain IS NOT NULL;
