-- 0045 — Rimettere i valori predefiniti persi nella 0044.
--
-- La 0027 dichiarava `expire_stale_jobs(p_grace_hours integer DEFAULT 6,
-- p_batch_size integer DEFAULT 5000)`. I default non sono decorazione: in
-- Postgres rendono la funzione chiamabile anche con meno argomenti, quindi
-- `expire_stale_jobs(6)` era una firma valida — e check_constraints la usa.
--
-- Ricreando la funzione nella 0044 li ho persi, e le chiamate a un argomento
-- hanno smesso di esistere. Il difetto della finestra era giusto; questa è
-- la parte che ho rotto sistemandolo, trovata dai vincoli e non a occhio.
DROP FUNCTION IF EXISTS expire_stale_jobs(integer, integer);

CREATE FUNCTION expire_stale_jobs(p_grace_hours integer DEFAULT 6,
                                  p_batch_size integer DEFAULT 5000)
RETURNS TABLE (motivo text, righe bigint)
LANGUAGE plpgsql AS $$
DECLARE n bigint;
BEGIN
  UPDATE jobs SET status = 'expired', expired_at = now()
   WHERE ctid IN (
     SELECT ctid FROM jobs
      WHERE status = 'active'
        AND date_valid_through IS NOT NULL
        AND date_valid_through < now()
      LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  motivo := 'scadenza dichiarata'; righe := n; RETURN NEXT;

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
                                 + make_interval(hours => p_grace_hours)
             -- La finestra della fetch deve contenere la data di
             -- pubblicazione: una fetch incrementale non ha mai guardato
             -- dove sta l'offerta vecchia, quindi non puo' dichiararla morta.
             AND COALESCE((r.request_params->>'since')::timestamptz,
                          '-infinity'::timestamptz) <= j.date_posted)
      LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  motivo := 'non più vista in una fetch che la copriva'; righe := n; RETURN NEXT;
  RETURN;
END $$;

COMMENT ON FUNCTION expire_stale_jobs(integer, integer) IS
  'Deduce la scadenza solo da una fetch completa LA CUI FINESTRA copre la '
  'data di pubblicazione. `fetch_complete` da solo dice che la paginazione '
  'non e'' stata troncata, non che la fetch abbia elencato tutto.';
