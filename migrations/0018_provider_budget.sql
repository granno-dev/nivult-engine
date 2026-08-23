-- 0018 — Budget per fornitore e conciliazione dei crediti.
--
-- Due buchi nella contabilità, che si vedono solo con una fonte a pagamento.
--
-- PRIMO: il costo non è noto prima della chiamata. Fantastic scala UN credito
-- per OFFERTA RESTITUITA, non per richiesta, quindi cluster_try_consume — che
-- vuole un numero prima di partire — non può sapere quanto sta autorizzando.
-- Si riserva il caso peggiore e si concilia dopo.
--
-- SECONDO, ed è il più pericoloso: il nostro tetto è per cluster e per giorno,
-- la quota del fornitore è per account e per mese. Venti cluster ciascuno
-- entro il proprio tetto giornaliero possono esaurire le 20.000 offerte del
-- mese in pochi giorni senza che nessun breaker scatti. Il tetto per cluster
-- protegge dalla singola query impazzita; questo protegge la fattura.

CREATE TABLE provider_quotas (
  provider              text    PRIMARY KEY,
  monthly_credits_cap   integer NOT NULL CHECK (monthly_credits_cap >= 0),
  monthly_requests_cap  integer NOT NULL CHECK (monthly_requests_cap >= 0),
  note                  text,
  updated_at            timestamptz NOT NULL DEFAULT now()
);

-- 0 significa "nessun costo": le fonti pubbliche restano contabilizzate ma non
-- possono aprire il breaker per esaurimento crediti.
INSERT INTO provider_quotas (provider, monthly_credits_cap, monthly_requests_cap, note) VALUES
  ('fantastic',          20000, 10000, 'Piano Starter-20k'),
  ('france_travail',         0,     0, 'gratuita: limite di frequenza, non di quota'),
  ('arbetsformedlingen',     0,     0, 'gratuita: limite di frequenza, non di quota');


-- Una riga per fornitore per mese, come cluster_daily_budget: il mese nuovo È
-- una riga nuova, niente azzeramento e niente contesa.
CREATE TABLE provider_budget (
  provider          text    NOT NULL REFERENCES provider_quotas(provider) ON DELETE CASCADE,
  period_month      date    NOT NULL,
  credits_used      integer NOT NULL DEFAULT 0 CHECK (credits_used >= 0),
  requests_used     integer NOT NULL DEFAULT 0 CHECK (requests_used >= 0),
  circuit_open      boolean NOT NULL DEFAULT false,
  circuit_opened_at timestamptz,
  circuit_reason    text,

  PRIMARY KEY (provider, period_month),
  CONSTRAINT provider_budget_month_ck
    CHECK (period_month = date_trunc('month', period_month)::date),
  CONSTRAINT provider_budget_circuit_ck
    CHECK (circuit_open = (circuit_opened_at IS NOT NULL))
);

CREATE INDEX provider_budget_open_idx ON provider_budget (period_month) WHERE circuit_open;


-- Riserva sul budget mensile del fornitore. Come cluster_try_consume:
-- false significa "non procedere".
CREATE OR REPLACE FUNCTION provider_try_consume(
  p_provider text, p_credits integer, p_requests integer DEFAULT 1)
RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  v_credits_cap  integer;
  v_requests_cap integer;
  v_credits      integer;
  v_requests     integer;
  v_month        date := date_trunc('month', current_date)::date;
  v_reason       text;
BEGIN
  IF p_credits < 0 OR p_requests < 0 THEN
    RAISE EXCEPTION 'valori negativi: crediti=%, richieste=%', p_credits, p_requests;
  END IF;

  SELECT monthly_credits_cap, monthly_requests_cap
    INTO v_credits_cap, v_requests_cap
    FROM provider_quotas WHERE provider = p_provider;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'fornitore senza quota configurata: %', p_provider;
  END IF;

  INSERT INTO provider_budget (provider, period_month) VALUES (p_provider, v_month)
  ON CONFLICT (provider, period_month) DO NOTHING;

  SELECT credits_used, requests_used INTO v_credits, v_requests
    FROM provider_budget
   WHERE provider = p_provider AND period_month = v_month
   FOR UPDATE;

  -- Un tetto a 0 vuol dire fonte gratuita: si contabilizza e si passa.
  IF v_credits_cap > 0 AND v_credits + p_credits > v_credits_cap THEN
    v_reason := format('crediti mensili esauriti (%s/%s)', v_credits, v_credits_cap);
  ELSIF v_requests_cap > 0 AND v_requests + p_requests > v_requests_cap THEN
    v_reason := format('richieste mensili esaurite (%s/%s)', v_requests, v_requests_cap);
  END IF;

  IF v_reason IS NOT NULL THEN
    UPDATE provider_budget
       SET circuit_open = true,
           circuit_opened_at = COALESCE(circuit_opened_at, now()),
           circuit_reason = COALESCE(circuit_reason, v_reason)
     WHERE provider = p_provider AND period_month = v_month;
    RETURN false;
  END IF;

  UPDATE provider_budget
     SET credits_used = credits_used + p_credits,
         requests_used = requests_used + p_requests
   WHERE provider = p_provider AND period_month = v_month;
  RETURN true;
END $$;


-- Conciliazione dopo la chiamata.
--
-- Si riserva il costo peggiore e poi si restituisce la differenza. Il verso
-- conta: riservare poco e aggiustare in su lascerebbe una finestra in cui due
-- worker paralleli sforano entrambi credendo di stare nel tetto.
-- delta negativo = rimborso. Non apre mai il breaker: è una correzione, non un
-- consumo.
CREATE OR REPLACE FUNCTION settle_credits(
  p_provider text, p_cluster_id uuid, p_delta integer)
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

  IF p_cluster_id IS NOT NULL THEN
    UPDATE cluster_daily_budget
       SET credits_used = GREATEST(0, credits_used + p_delta)
     WHERE cluster_id = p_cluster_id AND usage_date = current_date;
  END IF;
END $$;

COMMENT ON FUNCTION settle_credits(text, uuid, integer) IS
  'Concilia dopo una chiamata a costo variabile. delta negativo = rimborso. '
  'Non apre il breaker: è una correzione, non un consumo.';


CREATE VIEW provider_budget_v AS
SELECT b.provider, b.period_month,
       b.credits_used, q.monthly_credits_cap,
       CASE WHEN q.monthly_credits_cap > 0
            THEN round(100.0 * b.credits_used / q.monthly_credits_cap, 1) END AS credits_pct,
       b.requests_used, q.monthly_requests_cap,
       b.circuit_open, b.circuit_reason
FROM provider_budget b
JOIN provider_quotas q ON q.provider = b.provider;
