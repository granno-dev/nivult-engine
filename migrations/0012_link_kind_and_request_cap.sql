-- 0012 — Tipo di link, tetto di richieste, completezza della fetch.

-- Tipo di link.
--
-- La regola "solo career site aziendali" nasceva contro gli aggregatori
-- commerciali che rivendono traffico, non contro gli enti pubblici del lavoro.
-- Le agenzie nazionali vengono ammesse ma ETICHETTATE: la trasparenza verso
-- l'utente è ciò che permette di ammetterle senza tradire la promessa.
--
-- rank serve al funnel: a parità di punteggio, career_site viene prima nel
-- digest. Sta in tabella e non in codice perché è una politica di prodotto, e
-- cambiarla non deve richiedere un rilascio.
CREATE TABLE link_kinds (
  kind      text     PRIMARY KEY,
  rank      smallint NOT NULL UNIQUE CHECK (rank > 0),
  is_direct boolean  NOT NULL,
  note      text     NOT NULL
);

INSERT INTO link_kinds (kind, rank, is_direct, note) VALUES
  ('career_site',     1, true,  'Sito dell''azienda: candidatura diretta'),
  ('national_agency', 2, false, 'Ente pubblico del lavoro (France Travail, Bundesagentur, ...)'),
  ('job_board',       3, false, 'Bacheca o aggregatore commerciale (LinkedIn, ...)');

COMMENT ON COLUMN link_kinds.rank IS
  'Ordine di preferenza nel digest a parità di punteggio. Più basso = prima.';
COMMENT ON COLUMN link_kinds.is_direct IS
  'Se true il digest mostra "candidatura diretta", altrimenti "via <fonte>".';

-- NOT NULL senza default: il tipo di link va dichiarato a ogni inserimento.
-- Un default 'career_site' silenzioso trasformerebbe una svista in una bugia
-- all'utente — gli diremmo "candidatura diretta" per un link a un aggregatore.
-- La tabella è vuota, quindi il default serve solo ad aggiungere la colonna.
ALTER TABLE jobs ADD COLUMN link_kind text NOT NULL DEFAULT 'career_site'
  REFERENCES link_kinds(kind);
ALTER TABLE jobs ALTER COLUMN link_kind DROP DEFAULT;

-- Il funnel filtra e ordina sull'insieme vivo, quindi l'indice è parziale come
-- gli altri.
CREATE INDEX jobs_link_kind_idx ON jobs (link_kind)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;


-- Tetto di richieste giornaliere.
--
-- cluster_try_consume finora proteggeva dal consumo di denaro. Le fonti
-- pubbliche nazionali sono gratuite: credits = 0, quindi il breaker non
-- scatterebbe mai. Ma hanno un'altra risorsa scarsa, il rate limit, e superarlo
-- non costa soldi: costa un ban.
ALTER TABLE clusters ADD COLUMN daily_request_cap integer NOT NULL DEFAULT 500
  CHECK (daily_request_cap > 0);

COMMENT ON COLUMN clusters.daily_request_cap IS
  'Tetto di chiamate al giorno, indipendente dai crediti. Protegge dal ban, non dal costo.';


-- Il breaker ora controlla entrambi i tetti.
CREATE OR REPLACE FUNCTION cluster_try_consume(p_cluster_id uuid, p_credits integer)
RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  v_credit_cap  integer;
  v_request_cap integer;
  v_used        integer;
  v_requests    integer;
  v_reason      text;
BEGIN
  IF p_credits < 0 THEN
    RAISE EXCEPTION 'p_credits non può essere negativo: %', p_credits;
  END IF;

  SELECT daily_credit_cap, daily_request_cap
    INTO v_credit_cap, v_request_cap
    FROM clusters WHERE id = p_cluster_id;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'cluster sconosciuto: %', p_cluster_id;
  END IF;

  INSERT INTO cluster_daily_budget (cluster_id, usage_date)
  VALUES (p_cluster_id, current_date)
  ON CONFLICT (cluster_id, usage_date) DO NOTHING;

  SELECT credits_used, requests_made
    INTO v_used, v_requests
    FROM cluster_daily_budget
   WHERE cluster_id = p_cluster_id AND usage_date = current_date
   FOR UPDATE;

  IF v_used + p_credits > v_credit_cap THEN
    v_reason := format('tetto crediti raggiunto (%s/%s)', v_used, v_credit_cap);
  ELSIF v_requests + 1 > v_request_cap THEN
    v_reason := format('tetto richieste raggiunto (%s/%s)', v_requests, v_request_cap);
  END IF;

  IF v_reason IS NOT NULL THEN
    UPDATE cluster_daily_budget
       SET circuit_open      = true,
           circuit_opened_at = COALESCE(circuit_opened_at, now()),
           circuit_reason    = COALESCE(circuit_reason, v_reason)
     WHERE cluster_id = p_cluster_id AND usage_date = current_date;
    RETURN false;
  END IF;

  UPDATE cluster_daily_budget
     SET credits_used  = credits_used + p_credits,
         requests_made = requests_made + 1
   WHERE cluster_id = p_cluster_id AND usage_date = current_date;

  RETURN true;
END $$;


-- Completezza della fetch.
--
-- Serve allo sweep delle scadute. Se una fetch è stata troncata dal limite di
-- pagina, il fatto che un'offerta non sia comparsa NON significa che sia
-- scaduta: significa che non siamo arrivati a leggerla. Marcarle scadute
-- ucciderebbe offerte vive a caso, e solo per i cluster più grandi.
-- Solo le fetch complete danno diritto a marcare scadute.
ALTER TABLE ingestion_runs
  ADD COLUMN fetch_complete boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN ingestion_runs.fetch_complete IS
  'true solo se la fonte ha restituito tutti i risultati disponibili, senza '
  'troncamento da limite o paginazione. Prerequisito per lo sweep delle scadute.';
