-- 0040 — Il ruolo a cui l'utente punta, per ricerca.
--
-- La famiglia decide DOVE si legge; il ruolo dice COSA si sta cercando
-- davvero. Sono due cose diverse e vanno in due posti diversi.
--
-- Il ruolo NON diventa la chiave di ingestione, e la ragione è misurata (in
-- CLAUDE.md): title='Human Resources' dà 43 offerte in DE dove la tassonomia
-- ne dà 1.220. "HRBP", "People Partner" e "HR Business Partner" sono la
-- stessa ambizione scritta in dieci modi e cinque lingue: un cluster per
-- titolo perderebbe quasi tutto e distruggerebbe la condivisione — ogni
-- variante di titolo un cluster suo, ognuno coi suoi crediti.
--
-- Va invece al MODELLO, come il testo libero: in coda all'offerta, mai nel
-- prefisso in cache. Pesa sul punteggio; non esclude niente da solo — un
-- Finance Director e' un'ottima offerta anche per chi ha scritto Finance
-- Analyst, e dev'essere il punteggio a dirlo, non un filtro a nasconderla.
ALTER TABLE user_clusters
  ADD COLUMN target_role text
  CHECK (target_role IS NULL OR length(target_role) <= 120);

COMMENT ON COLUMN user_clusters.target_role IS
  'Il ruolo a cui punta, con parole sue ("HR Business Partner"). Segnale di '
  'punteggio per il modello, mai chiave di ingestione ne'' filtro secco.';
