-- 0007 — Cancellazione dell'utente su richiesta, a lotti.

-- Le FK sono quasi tutte ON DELETE CASCADE, quindi un DELETE sulla riga utente
-- si porterebbe dietro tutto lo storico in una transazione sola: un lock lungo
-- su tabelle calde, e un rollback che rimette tutto com'era se qualcosa va
-- storto a metà. Qui i figli si cancellano esplicitamente, a lotti piccoli, uno
-- per transazione.
--
-- La riga di richiesta sopravvive all'utente (niente FK): serve come prova di
-- avvenuta cancellazione. Contiene solo un identificativo opaco, nessun dato
-- personale.
CREATE TABLE deletion_requests (
  id                   uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid        NOT NULL,
  requested_at         timestamptz NOT NULL DEFAULT now(),
  status               text        NOT NULL DEFAULT 'pending'
                                   CHECK (status IN ('pending','running','completed','failed')),
  started_at           timestamptz,
  completed_at         timestamptz,
  rows_deleted         jsonb       NOT NULL DEFAULT '{}',
  -- Le chiavi object storage dei CV, raccolte PRIMA di cancellare le righe.
  -- Senza questo passaggio i blob resterebbero orfani e la cancellazione
  -- sarebbe incompleta.
  pending_storage_keys jsonb       NOT NULL DEFAULT '[]',
  error_message        text,

  CONSTRAINT deletion_requests_completed_ck
    CHECK ((status = 'completed') = (completed_at IS NOT NULL)),
  CONSTRAINT deletion_requests_failed_ck
    CHECK (status <> 'failed' OR error_message IS NOT NULL)
);

-- Una sola richiesta aperta per utente.
CREATE UNIQUE INDEX deletion_requests_open_idx ON deletion_requests (user_id)
  WHERE status IN ('pending','running');
CREATE INDEX deletion_requests_status_idx ON deletion_requests (status, requested_at);


-- Cancella al massimo p_batch_size righe dalla PRIMA tabella non vuota, in
-- ordine di dipendenza, e ritorna cosa ha cancellato. Il chiamante richiama
-- finché non riceve 'done'. Ogni chiamata è una transazione breve.
--
-- L'ordine non è negoziabile: digest_items referenzia sia digests sia matches,
-- quindi va per prima.
CREATE OR REPLACE FUNCTION delete_user_batch(p_user_id uuid, p_batch_size integer DEFAULT 5000)
RETURNS TABLE (step text, rows_affected bigint)
LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
BEGIN
  IF p_batch_size <= 0 THEN
    RAISE EXCEPTION 'p_batch_size deve essere positivo, ricevuto %', p_batch_size;
  END IF;

  DELETE FROM digest_items WHERE ctid IN (
    SELECT ctid FROM digest_items WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'digest_items'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM digests WHERE ctid IN (
    SELECT ctid FROM digests WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'digests'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM matches WHERE ctid IN (
    SELECT ctid FROM matches WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'matches'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM user_clusters WHERE ctid IN (
    SELECT ctid FROM user_clusters WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'user_clusters'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM user_cvs WHERE ctid IN (
    SELECT ctid FROM user_cvs WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'user_cvs'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  -- Non si cancella: si recide il collegamento. Il costo sostenuto resta
  -- contabilizzato, ma non è più attribuibile a una persona.
  UPDATE api_usage SET user_id = NULL WHERE ctid IN (
    SELECT ctid FROM api_usage WHERE user_id = p_user_id LIMIT p_batch_size);
  GET DIAGNOSTICS n = ROW_COUNT;
  IF n > 0 THEN step := 'api_usage:anonimizzate'; rows_affected := n; RETURN NEXT; RETURN; END IF;

  DELETE FROM users WHERE id = p_user_id;
  GET DIAGNOSTICS n = ROW_COUNT;
  step := 'users'; rows_affected := n; RETURN NEXT;
  RETURN;
END $$;

COMMENT ON FUNCTION delete_user_batch(uuid, integer) IS
  'Un lotto di cancellazione. Richiamare finché step non è ''users''. Vedi nivult.gdpr.';
