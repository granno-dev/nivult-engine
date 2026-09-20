-- Il dominio delle carriere non e' il dominio aziendale (19/09/2026).
--
-- `carrieres-mousquetaires.com` invece di `mousquetaires.com`,
-- `accorhotels-ausbildung.de` invece di `accor.com`: 561 su 4.678 (11,3%).
-- La prova dell'aggancio regge — quel sito rimanda davvero al nostro tenant — ma
-- chi compra vuole il dominio aziendale per incrociarlo col proprio CRM.
--
-- Qui si SEGNALA soltanto. Il dominio giusto si ricava togliendo il pezzo che
-- parla di lavoro, ma «carrieres-mousquetaires.com» meno «carrieres» fa
-- «mousquetaires.com» solo se quel dominio esiste ED e' della stessa azienda:
-- scriverlo senza verificarlo sarebbe indovinare, ed e' esattamente l'errore che
-- oggi ci e' costato 492 domini falsi.
ALTER TABLE aziende_vendibili ADD COLUMN IF NOT EXISTS dominio_e_carriere boolean NOT NULL DEFAULT false;

UPDATE aziende_vendibili
   SET dominio_e_carriere = (dominio IS NOT NULL AND dominio ~*
       '(carrier|career|jobs?[-.]|[-.]jobs?|lavora|ausbildung|recruit|hiring|talent|werkenbij|empleo|karriere|stage)');

-- La vista di cio' che si vende resta invariata: un dominio delle carriere e'
-- comunque un aggancio buono, solo meno comodo. Si segnala, non si scarta.
