-- 0081 — ripara i contratti dopo l'apertura a piu' termini per fonte (0080).
--
-- La PK ora include `query`: i conflitti devono usare tutte e tre le colonne.
-- La vista di copertura resta invece per cluster-fonte, aggregando i termini.
-- Non si creano nuovi termini dai filtri degli utenti.

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
    UPDATE clusters SET status = 'active', updated_at = now()
     WHERE id = v_id AND status <> 'active';
  ELSE
    INSERT INTO clusters (family, country) VALUES (p_family, p_country)
    ON CONFLICT (family, country) DO UPDATE SET updated_at = now()
    RETURNING id INTO v_id;
  END IF;

  INSERT INTO cluster_source_queries (cluster_id, source, query, note)
  VALUES (v_id, 'linkedin', p_family, 'seminato da apri_cluster'),
         (v_id, 'indeed', p_family, 'seminato da apri_cluster')
  ON CONFLICT (cluster_id, source, query) DO NOTHING;

  RETURN v_id;
END;
$$;


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
       (count(q.query) > 0) AS interrogabile,
       string_agg(q.query, ' | ' ORDER BY q.query) AS query
FROM clusters c
JOIN fonti_per_paese f ON f.country = c.country
LEFT JOIN cluster_source_queries q
       ON q.cluster_id = c.id AND q.source = f.source
GROUP BY c.id, c.family, c.country, f.source;

COMMENT ON VIEW cluster_coverage_v IS
  'Una riga per cluster-fonte. interrogabile = almeno un termine configurato; '
  'query contiene tutti i termini in ordine, non soltanto il primo.';


-- Le richieste hanno lo stesso ciclo riserva-poi-concilia dei crediti. Serve
-- sia quando il cluster rifiuta dopo che il provider ha gia' concesso, sia
-- quando retry, token o dettagli producono piu' chiamate della pagina riservata.
CREATE OR REPLACE FUNCTION settle_requests(
  p_provider text, p_cluster_id uuid, p_delta integer,
  p_backfill boolean DEFAULT false)
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF p_delta = 0 THEN
    RETURN;
  END IF;

  UPDATE provider_budget
     SET requests_used = GREATEST(0, requests_used + p_delta)
   WHERE provider = p_provider
     AND period_month = date_trunc('month', current_date)::date;

  IF p_cluster_id IS NULL THEN
    RETURN;
  END IF;

  IF p_backfill THEN
    UPDATE clusters
       SET backfill_requests_used = GREATEST(0, backfill_requests_used + p_delta)
     WHERE id = p_cluster_id;
  ELSE
    UPDATE cluster_daily_budget
       SET requests_made = GREATEST(0, requests_made + p_delta)
     WHERE cluster_id = p_cluster_id AND usage_date = current_date;
  END IF;
END $$;

COMMENT ON FUNCTION settle_requests(text, uuid, integer, boolean) IS
  'Concilia le richieste reali dopo la riserva. delta negativo = rimborso; '
  'non apre il breaker e non scende mai sotto zero.';
