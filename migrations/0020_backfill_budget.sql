-- 0020 — Budget dedicato al backfill.
--
-- Il primo giro di un cluster nuovo scarica due settimane di storico: molte
-- più offerte di un giorno normale. Se attingesse dal tetto giornaliero, ogni
-- cluster nuovo aprirebbe il breaker al primo colpo e resterebbe a metà.
--
-- Quindi una dotazione separata, una tantum. NON è un'esenzione dai soldi:
-- provider_budget continua a valere, perché quello è la fattura. Il backfill è
-- esente solo dal tetto GIORNALIERO DEL CLUSTER, che serve a un'altra cosa —
-- accorgersi di una query impazzita.

ALTER TABLE clusters
  ADD COLUMN backfill_credit_cap    integer NOT NULL DEFAULT 2000
    CHECK (backfill_credit_cap >= 0),
  ADD COLUMN backfill_request_cap   integer NOT NULL DEFAULT 60
    CHECK (backfill_request_cap >= 0),
  ADD COLUMN backfill_credits_used  integer NOT NULL DEFAULT 0
    CHECK (backfill_credits_used >= 0),
  ADD COLUMN backfill_requests_used integer NOT NULL DEFAULT 0
    CHECK (backfill_requests_used >= 0),
  ADD COLUMN backfill_completed_at  timestamptz,
  ADD COLUMN backfill_truncated     boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN clusters.backfill_completed_at IS
  'NULL = il cluster è ancora in backfill e attinge alla dotazione dedicata.';
COMMENT ON COLUMN clusters.backfill_truncated IS
  'true se il backfill si è chiuso per dotazione esaurita invece che per aver '
  'visto tutto. Il cluster parte con uno storico incompleto: va saputo.';


-- Funzione separata invece di un parametro su cluster_try_consume: aggiungere
-- un argomento con default creerebbe due firme ambigue per le chiamate a due
-- argomenti già esistenti.
CREATE OR REPLACE FUNCTION cluster_try_consume_backfill(
  p_cluster_id uuid, p_credits integer)
RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  v_credit_cap  integer;
  v_request_cap integer;
  v_credits     integer;
  v_requests    integer;
  v_done        timestamptz;
BEGIN
  IF p_credits < 0 THEN
    RAISE EXCEPTION 'p_credits non può essere negativo: %', p_credits;
  END IF;

  SELECT backfill_credit_cap, backfill_request_cap,
         backfill_credits_used, backfill_requests_used, backfill_completed_at
    INTO v_credit_cap, v_request_cap, v_credits, v_requests, v_done
    FROM clusters WHERE id = p_cluster_id FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'cluster sconosciuto: %', p_cluster_id;
  END IF;

  -- Il backfill è una tantum: a cluster già inizializzato si usa il tetto
  -- giornaliero come per tutti gli altri.
  IF v_done IS NOT NULL THEN
    RETURN false;
  END IF;

  IF v_credits + p_credits > v_credit_cap OR v_requests + 1 > v_request_cap THEN
    RETURN false;
  END IF;

  UPDATE clusters
     SET backfill_credits_used  = backfill_credits_used + p_credits,
         backfill_requests_used = backfill_requests_used + 1
   WHERE id = p_cluster_id;
  RETURN true;
END $$;


-- Chiude il backfill. p_truncated dice se si è fermato per dotazione esaurita
-- invece che per aver visto tutto lo storico.
CREATE OR REPLACE FUNCTION cluster_finish_backfill(
  p_cluster_id uuid, p_truncated boolean)
RETURNS void
LANGUAGE sql AS $$
  UPDATE clusters
     SET backfill_completed_at = COALESCE(backfill_completed_at, now()),
         backfill_truncated = p_truncated
   WHERE id = p_cluster_id;
$$;


-- La conciliazione deve sapere da quale borsellino restituire.
--
-- La versione a tre argomenti va ELIMINATA, non sostituita: CREATE OR REPLACE
-- con un parametro in più crea una funzione nuova accanto alla vecchia, e ogni
-- chiamata a tre argomenti diventa ambigua. È la stessa ragione per cui
-- cluster_try_consume_backfill è una funzione a sé invece di un parametro.
DROP FUNCTION IF EXISTS settle_credits(text, uuid, integer);

CREATE OR REPLACE FUNCTION settle_credits(
  p_provider text, p_cluster_id uuid, p_delta integer, p_backfill boolean DEFAULT false)
RETURNS void
LANGUAGE plpgsql AS $$
BEGIN
  IF p_delta = 0 THEN
    RETURN;
  END IF;

  UPDATE provider_budget
     SET credits_used = GREATEST(0, credits_used + p_delta)
   WHERE provider = p_provider
     AND period_month = date_trunc('month', current_date)::date;

  IF p_cluster_id IS NULL THEN
    RETURN;
  END IF;

  IF p_backfill THEN
    UPDATE clusters
       SET backfill_credits_used = GREATEST(0, backfill_credits_used + p_delta)
     WHERE id = p_cluster_id;
  ELSE
    UPDATE cluster_daily_budget
       SET credits_used = GREATEST(0, credits_used + p_delta)
     WHERE cluster_id = p_cluster_id AND usage_date = current_date;
  END IF;
END $$;


CREATE VIEW cluster_backfill_v AS
SELECT c.id, c.family, c.country,
       (c.backfill_completed_at IS NULL) AS in_corso,
       c.backfill_credits_used, c.backfill_credit_cap,
       c.backfill_requests_used, c.backfill_request_cap,
       c.backfill_completed_at, c.backfill_truncated
FROM clusters c;
