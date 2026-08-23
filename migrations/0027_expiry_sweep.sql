-- 0027 — Sweep delle offerte scadute.
--
-- Tre segnali, in ordine di affidabilità:
--
--   1. la fonte lo dichiara   -> 'removed'. Arbetsförmedlingen col JobStream,
--      Fantastic con /expired-ats. Entrambi gratuiti, entrambi certi.
--   2. date_valid_through superata -> 'expired'. Certo ma raro: il campo c'è
--      sul 15% delle offerte.
--   3. non più vista           -> 'expired'. Deduzione, e l'unica delle tre che
--      può sbagliare.
--
-- Il terzo ha una condizione che non è negoziabile: vale SOLO se la fetch che
-- avrebbe dovuto restituirla era COMPLETA. Su una fetch troncata dal tetto o
-- dal limite di scorrimento della fonte, l'assenza di un'offerta non significa
-- che sia sparita — significa che non siamo arrivati a leggerla. Senza questa
-- condizione si ucciderebbero offerte vive, e sistematicamente quelle dei
-- cluster più grandi, che sono quelli che si troncano.

CREATE OR REPLACE FUNCTION mark_jobs_removed(p_source text, p_source_ids text[])
RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
  UPDATE jobs
     SET status = 'removed', expired_at = now()
   WHERE source = p_source
     AND source_job_id = ANY(p_source_ids)
     AND status = 'active';
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

COMMENT ON FUNCTION mark_jobs_removed(text, text[]) IS
  '''removed'' e non ''expired'': la fonte dice esplicitamente che non c''è più, '
  'non lo stiamo deducendo.';


CREATE OR REPLACE FUNCTION expire_stale_jobs(
  p_grace_hours integer DEFAULT 6, p_batch_size integer DEFAULT 5000)
RETURNS TABLE (motivo text, righe bigint)
LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
  IF p_grace_hours < 0 OR p_batch_size <= 0 THEN
    RAISE EXCEPTION 'parametri non validi';
  END IF;

  -- 1. Scadenza dichiarata dall'offerta stessa.
  UPDATE jobs SET status = 'expired', expired_at = now()
   WHERE ctid IN (
     SELECT ctid FROM jobs
      WHERE status = 'active'
        AND date_valid_through IS NOT NULL
        AND date_valid_through < now()
      LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  motivo := 'scadenza dichiarata'; righe := n; RETURN NEXT;

  -- 2. Non più vista, ma solo dove abbiamo il diritto di dedurlo: una fetch
  --    COMPLETA della stessa fonte, su un cluster a cui l'offerta appartiene,
  --    conclusa dopo l'ultima volta che l'abbiamo vista viva.
  UPDATE jobs SET status = 'expired', expired_at = now()
   WHERE ctid IN (
     SELECT j.ctid FROM jobs j
      WHERE j.status = 'active'
        AND EXISTS (
          SELECT 1
            FROM job_clusters jc
            JOIN ingestion_runs r ON r.cluster_id = jc.cluster_id
           WHERE jc.job_id = j.id
             AND r.source = j.source
             AND r.status = 'success'
             AND r.fetch_complete
             AND r.finished_at > j.last_seen_alive_at
                                 + make_interval(hours => p_grace_hours))
      LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  motivo := 'non più vista in una fetch completa'; righe := n; RETURN NEXT;
  RETURN;
END $$;

COMMENT ON FUNCTION expire_stale_jobs(integer, integer) IS
  'Deduce la scadenza SOLO da fetch complete. Su una fetch troncata l''assenza '
  'di un''offerta non significa niente.';


-- Quante offerte sono a rischio di deduzione sbagliata: appartengono a cluster
-- la cui ultima fetch era troncata, quindi non possiamo dire nulla su di loro.
CREATE VIEW expiry_blind_spots_v AS
SELECT c.family, c.country, r.source,
       count(DISTINCT jc.job_id) AS offerte_non_giudicabili,
       max(r.finished_at)        AS ultima_fetch
FROM ingestion_runs r
JOIN job_clusters jc ON jc.cluster_id = r.cluster_id
JOIN jobs j          ON j.id = jc.job_id AND j.status = 'active'
JOIN clusters c      ON c.id = r.cluster_id
WHERE r.status = 'success' AND NOT r.fetch_complete
GROUP BY c.family, c.country, r.source;

COMMENT ON VIEW expiry_blind_spots_v IS
  'Cluster le cui fetch si troncano: lì le scadute non si possono dedurre, e le '
  'offerte morte restano attive finché la fonte non le dichiara.';
