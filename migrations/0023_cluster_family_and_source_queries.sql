-- 0023 — Il cluster è una famiglia, le fonti lo interrogano ciascuna a modo suo.
--
-- Finora clusters.family era un termine di ricerca ("ressources humaines"), e
-- quel termine finiva tale e quale a ogni fonte. Non funziona: Fantastic sa
-- filtrare per tassonomia, France Travail vuole parole francesi,
-- Arbetsförmedlingen svedesi. Un solo campo non può essere tutte e tre le cose.
--
-- Ora l'identità del cluster è la FAMIGLIA — un valore di job_families, cioè la
-- tassonomia del fornitore — e i termini di ricerca per le fonti che quella
-- tassonomia non ce l'hanno stanno a parte.
--
-- La FK fallisce se esistono cluster con famiglie fuori vocabolario. È voluto:
-- meglio una migrazione che si ferma di una che silenziosamente lascia cluster
-- che nessuna fonte saprà interrogare.

ALTER TABLE clusters
  ADD CONSTRAINT clusters_family_fk FOREIGN KEY (family) REFERENCES job_families(family);

COMMENT ON COLUMN clusters.family IS
  'Famiglia professionale, valore di job_families (= ai_taxonomies_a). '
  'NON è un termine di ricerca: quelli stanno in cluster_source_queries.';


CREATE TABLE cluster_source_queries (
  cluster_id uuid        NOT NULL REFERENCES clusters(id) ON DELETE CASCADE,
  source     text        NOT NULL CHECK (source IN (
                           'fantastic','france_travail','bundesagentur',
                           'arbetsformedlingen','nav','tyomarkkinatori')),
  query      text        NOT NULL CHECK (length(btrim(query)) > 0),
  note       text,
  created_at timestamptz NOT NULL DEFAULT now(),

  PRIMARY KEY (cluster_id, source)
);

COMMENT ON TABLE cluster_source_queries IS
  'Termine di ricerca per le fonti che non sanno filtrare per tassonomia. '
  'Fantastic non ne ha bisogno: chiede ai_taxonomies_a e basta.';

CREATE INDEX cluster_source_queries_source_idx ON cluster_source_queries (source);


-- Un cluster senza termine per una fonte che copre il suo paese verrebbe
-- semplicemente saltato da quella fonte, in silenzio. Questa vista lo mostra.
CREATE VIEW cluster_coverage_v AS
WITH fonti_per_paese AS (
  SELECT 'FR'::text AS country, 'france_travail'::text AS source
  UNION ALL SELECT 'SE', 'arbetsformedlingen'
)
SELECT c.id, c.family, c.country, f.source,
       (q.query IS NOT NULL) AS interrogabile,
       q.query
FROM clusters c
JOIN fonti_per_paese f ON f.country = c.country
LEFT JOIN cluster_source_queries q ON q.cluster_id = c.id AND q.source = f.source;

COMMENT ON VIEW cluster_coverage_v IS
  'Quali fonti nazionali possono davvero interrogare ciascun cluster. '
  'interrogabile = false significa che quella fonte lo salterà in silenzio.';
