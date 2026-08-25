-- 0044 — Una fetch può dichiarare sparita solo ciò che era nella sua finestra.
--
-- Lo sweep deduceva la scadenza da qualunque fetch con `fetch_complete`. Ma
-- quel flag dice una cosa sola: che la PAGINAZIONE non è stata troncata. Non
-- dice affatto che la fetch abbia elencato tutte le offerte aperte del
-- cluster — e la fetch notturna è incrementale, chiede una finestra recente
-- (`since`), quindi non rivede mai un annuncio pubblicato dieci giorni fa e
-- ancora aperto.
--
-- Il risultato, misurato in produzione la notte del 25 agosto: 111 offerte
-- nuove arrivate, 2.753 dichiarate scadute. Il corpus è passato da ~2.850
-- attive a 193. Le offerte uccise erano pubblicate fra il 9 e il 24 agosto,
-- cioè tutte fuori dalla finestra della fetch che le ha condannate.
--
-- La regola giusta era già scritta in CLAUDE.md — "il terzo segnale vale solo
-- dopo una fetch completa" — ma l'implementazione ha tradotto "completa" in
-- "non troncata", che è un'altra cosa. Un'offerta del 9 agosto può essere
-- dichiarata sparita SOLO da una fetch la cui finestra copre il 9 agosto.
-- Il nome delle colonne di ritorno cambia, e Postgres non lo permette con
-- un semplice REPLACE: si elimina e si ricrea.
DROP FUNCTION IF EXISTS expire_stale_jobs(integer, integer);

CREATE FUNCTION expire_stale_jobs(p_grace_hours integer,
                                             p_batch_size integer)
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

  -- 2. Non più vista — e adesso anche: da una fetch che POTEVA vederla.
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
             -- pubblicazione. Senza `since` la fetch non aveva finestra e
             -- copriva tutto: -infinity le fa passare tutte, che è il
             -- comportamento di prima solo dove è davvero giustificato.
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
  'non e'' stata troncata, non che la fetch abbia elencato tutto: su una '
  'fetch incrementale ogni offerta piu'' vecchia della finestra sembrerebbe '
  'sparita, e il 2026-08-25 ne ha uccise 2.753 in una notte.';
