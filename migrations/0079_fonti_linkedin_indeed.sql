-- 0079 — `linkedin` e `indeed` entrano nel vocabolario delle fonti.
--
-- A differenza di `ats` (0051), queste DUE sono fonti vere di ricerca:
-- interrogate per termine, paginate, con un cursore per cluster. Quindi si
-- aprono TUTTI i vocabolari, compresi cluster_source_queries e
-- cluster_source_cursors — che per `ats` restarono chiusi apposta.
--
-- Il termine di ricerca nasce da solo: la famiglia professionale (inglese,
-- lo stesso vocabolario di job_families) funziona su entrambe. apri_cluster
-- lo semina per i cluster futuri; l'INSERT qui sotto copre quelli attivi.
-- Il ritmo gentile sta nei client (2-5s fra richieste, max 20-25 risultati
-- a query): per questo la quota e' a zero — contabilizzate, mai bloccanti.

ALTER TABLE jobs DROP CONSTRAINT jobs_source_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_source_check CHECK (
    source = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                        'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                        'ats', 'linkedin', 'indeed'])
);

ALTER TABLE ingestion_runs DROP CONSTRAINT ingestion_runs_source_check;
ALTER TABLE ingestion_runs ADD CONSTRAINT ingestion_runs_source_check CHECK (
    source = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                        'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                        'ats', 'linkedin', 'indeed'])
);

ALTER TABLE api_usage DROP CONSTRAINT api_usage_provider_check;
ALTER TABLE api_usage ADD CONSTRAINT api_usage_provider_check CHECK (
    provider = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                          'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                          'glm', 'bge-m3', 'bge-reranker', 'ats',
                          'linkedin', 'indeed'])
);

ALTER TABLE cluster_source_queries DROP CONSTRAINT cluster_source_queries_source_check;
ALTER TABLE cluster_source_queries ADD CONSTRAINT cluster_source_queries_source_check CHECK (
    source = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                        'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                        'linkedin', 'indeed'])
);

ALTER TABLE cluster_source_cursors DROP CONSTRAINT cluster_source_cursors_source_check;
ALTER TABLE cluster_source_cursors ADD CONSTRAINT cluster_source_cursors_source_check CHECK (
    source = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                        'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                        'linkedin', 'indeed'])
);

INSERT INTO provider_quotas (provider, monthly_credits_cap, monthly_requests_cap, note) VALUES
  ('linkedin', 0, 0, 'gratuita: endpoint guest pubblici, limite di frequenza'),
  ('indeed',   0, 0, 'gratuita: API mobile pubblica, limite di frequenza')
ON CONFLICT (provider) DO NOTHING;

-- I cluster ATTIVI di oggi ricevono il termine: la famiglia, che su
-- LinkedIn e Indeed vale come query. ON CONFLICT perche' una riga scritta
-- a mano con un termine migliore vince sul default.
INSERT INTO cluster_source_queries (cluster_id, source, query, note)
SELECT c.id, f.source, c.family, 'seminato dalla 0079: la famiglia come termine'
FROM clusters c
CROSS JOIN (VALUES ('linkedin'), ('indeed')) AS f(source)
WHERE c.status = 'active'
ON CONFLICT (cluster_id, source) DO NOTHING;

-- Stessa semina per i cluster che apriranno domani.
CREATE OR REPLACE FUNCTION apri_cluster(p_family text, p_country char(2))
RETURNS uuid
LANGUAGE plpgsql
AS $$
DECLARE
  v_id uuid;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM job_families WHERE family = p_family) THEN
    RAISE EXCEPTION 'famiglia professionale sconosciuta: %', p_family
      USING ERRCODE = 'check_violation';
  END IF;

  SELECT id INTO v_id FROM clusters
   WHERE family = p_family AND country = p_country;

  IF v_id IS NOT NULL THEN
    -- Un cluster archiviato torna attivo invece di essere duplicato: la
    -- UNIQUE lo vieterebbe comunque, e le offerte già scaricate restano.
    UPDATE clusters SET status = 'active', updated_at = now()
     WHERE id = v_id AND status <> 'active';
  ELSE
    INSERT INTO clusters (family, country) VALUES (p_family, p_country)
    ON CONFLICT (family, country) DO UPDATE SET updated_at = now()
    RETURNING id INTO v_id;
  END IF;

  -- Il termine per le fonti che cercano per parola: senza, il runner le
  -- salterebbe in silenzio («nessun termine di ricerca»). La famiglia e'
  -- inglese, che e' la forma che entrambe risolvono meglio.
  INSERT INTO cluster_source_queries (cluster_id, source, query, note)
  VALUES (v_id, 'linkedin', p_family, 'seminato da apri_cluster'),
         (v_id, 'indeed', p_family, 'seminato da apri_cluster')
  ON CONFLICT (cluster_id, source) DO NOTHING;

  RETURN v_id;
END;
$$;

-- La copertura ora include le due fonti nuove, sui mercati che coprono
-- (la stessa mappa dei client Python). Un cluster senza riga in
-- cluster_source_queries per una di loro appare «non interrogabile»
-- invece di sparire dalla vista.
CREATE OR REPLACE VIEW cluster_coverage_v AS
WITH fonti_per_paese AS (
  SELECT 'FR'::text AS country, 'france_travail'::text AS source
  UNION ALL SELECT 'SE', 'arbetsformedlingen'
  UNION ALL
  SELECT p.country, f.source
  FROM (VALUES ('FR'),('DE'),('IT'),('ES'),('SE'),('NO'),('FI'),('NL'),
               ('BE'),('AT'),('PT'),('IE'),('DK'),('PL'),('CH'))
       AS p(country)
  CROSS JOIN (VALUES ('linkedin'), ('indeed')) AS f(source)
)
SELECT c.id, c.family, c.country, f.source,
       (q.query IS NOT NULL) AS interrogabile,
       q.query
FROM clusters c
JOIN fonti_per_paese f ON f.country = c.country
LEFT JOIN cluster_source_queries q ON q.cluster_id = c.id AND q.source = f.source;

COMMENT ON VIEW cluster_coverage_v IS
  'Quali fonti possono davvero interrogare ciascun cluster. '
  'interrogabile = false significa che quella fonte lo salterà in silenzio.';
