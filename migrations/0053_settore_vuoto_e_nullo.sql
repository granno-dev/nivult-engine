-- 0053 — La stringa vuota in org_industry diventa NULL, e non torna più.
--
-- Scoperta da un test dei filtri: il «settore» più raro del corpus era ''.
-- Un'offerta con org_industry = '' non è NULL, quindi chi filtra per
-- settore la ESCLUDE — ma quel settore non è «diverso da quelli scelti»,
-- è semplicemente ignoto. È la regola «un campo assente non esclude mai»
-- aggirata da un'assenza travestita, come il false del visto (0052).
--
-- Tre righe oggi; il CHECK è perché non tornino. La fonte è stata
-- corretta insieme (fantastic.py fa NULLIF in ingresso), e il vincolo
-- garantisce che qualunque fonte futura dimentichi la lezione fallisca
-- rumorosamente invece di escludere offerte in silenzio.
UPDATE jobs SET org_industry = NULL WHERE org_industry = '';

ALTER TABLE jobs ADD CONSTRAINT jobs_org_industry_non_vuota_ck
  CHECK (org_industry IS NULL OR org_industry <> '');
