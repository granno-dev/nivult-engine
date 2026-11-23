-- 0080: piu' termini di ricerca per la stessa fonte sullo stesso cluster.
--
-- Nato con un termine solo per fonte (0079): la famiglia. Ma il recall vero
-- sta nei RUOLI obiettivo ("HR Operations Manager" trova piu' in fondo di
-- "Human Resources"): la chiave passa a (cluster, fonte, termine), cosi'
-- famiglia e ruoli convivono. Nessun dato toccato: la PK si rifa e basta.
BEGIN;

ALTER TABLE cluster_source_queries
  DROP CONSTRAINT cluster_source_queries_pkey;
ALTER TABLE cluster_source_queries
  ADD PRIMARY KEY (cluster_id, source, query);

COMMIT;
