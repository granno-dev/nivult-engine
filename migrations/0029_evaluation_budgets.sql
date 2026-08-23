-- 0029 — Budget di valutazione e valvola per i cluster sovradimensionati.
--
-- L'architettura cambia: GLM valuta direttamente tutte le offerte del cluster,
-- senza embedding né reranker. Il costo diventa quindi lineare nel numero di
-- offerte, e due cose che prima erano implicite adesso vanno presidiate.

-- ── 1. Quanto può costare un utente ────────────────────────────────────────
--
-- Stessa forma dei budget che già abbiamo: configurazione a parte, stato in una
-- riga per periodo. Il mese nuovo È una riga nuova, quindi niente azzeramento.
CREATE TABLE plan_quotas (
  plan                 text     PRIMARY KEY
                                CHECK (plan IN ('basic','pro','ultra')),
  monthly_evaluations  integer  NOT NULL CHECK (monthly_evaluations > 0),
  note                 text
);

-- Riferimento misurato: un cluster stretto fa ~300 offerte al mese, e un Ultra
-- su cluster stretti costa ~2,50 $/mese con GLM. I tetti lasciano margine
-- perché un utente può seguire più cluster.
INSERT INTO plan_quotas (plan, monthly_evaluations, note) VALUES
  ('basic',  1500, 'circa 5 cluster stretti'),
  ('pro',    5000, 'circa 15 cluster stretti'),
  ('ultra', 15000, 'circa 50 cluster stretti');


CREATE TABLE user_evaluation_budget (
  user_id           uuid    NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  period_month      date    NOT NULL,
  evaluations_used  integer NOT NULL DEFAULT 0 CHECK (evaluations_used >= 0),
  prescreened_used  integer NOT NULL DEFAULT 0 CHECK (prescreened_used >= 0),
  circuit_open      boolean NOT NULL DEFAULT false,
  circuit_opened_at timestamptz,
  circuit_reason    text,

  PRIMARY KEY (user_id, period_month),
  CONSTRAINT user_eval_month_ck
    CHECK (period_month = date_trunc('month', period_month)::date),
  CONSTRAINT user_eval_circuit_ck
    CHECK (circuit_open = (circuit_opened_at IS NOT NULL))
);

CREATE INDEX user_evaluation_budget_open_idx ON user_evaluation_budget (period_month)
  WHERE circuit_open;

COMMENT ON COLUMN user_evaluation_budget.prescreened_used IS
  'Pre-screening con un modello piccolo: contato a parte perché costa un ordine '
  'di grandezza meno e non deve consumare la quota di valutazione vera.';


-- Il breaker per utente. Stessa firma e stessa semantica degli altri:
-- false significa "non procedere".
CREATE OR REPLACE FUNCTION user_try_evaluate(p_user_id uuid, p_count integer)
RETURNS boolean
LANGUAGE plpgsql AS $$
DECLARE
  v_cap   integer;
  v_used  integer;
  v_month date := date_trunc('month', current_date)::date;
BEGIN
  IF p_count < 0 THEN
    RAISE EXCEPTION 'p_count non può essere negativo: %', p_count;
  END IF;

  SELECT q.monthly_evaluations INTO v_cap
    FROM users u JOIN plan_quotas q ON q.plan = u.plan
   WHERE u.id = p_user_id AND u.status = 'active';
  IF NOT FOUND THEN
    -- Utente inesistente, cancellato o sospeso: non si valuta.
    RETURN false;
  END IF;

  INSERT INTO user_evaluation_budget (user_id, period_month)
  VALUES (p_user_id, v_month)
  ON CONFLICT (user_id, period_month) DO NOTHING;

  SELECT evaluations_used INTO v_used
    FROM user_evaluation_budget
   WHERE user_id = p_user_id AND period_month = v_month
   FOR UPDATE;

  IF v_used + p_count > v_cap THEN
    UPDATE user_evaluation_budget
       SET circuit_open = true,
           circuit_opened_at = COALESCE(circuit_opened_at, now()),
           circuit_reason = COALESCE(circuit_reason,
             format('valutazioni mensili esaurite (%s/%s, piano %s)',
                    v_used, v_cap,
                    (SELECT plan FROM users WHERE id = p_user_id)))
     WHERE user_id = p_user_id AND period_month = v_month;
    RETURN false;
  END IF;

  UPDATE user_evaluation_budget
     SET evaluations_used = evaluations_used + p_count
   WHERE user_id = p_user_id AND period_month = v_month;
  RETURN true;
END $$;


-- ── 2. La valvola per i mercati grossi ─────────────────────────────────────
--
-- Un cluster stretto fa ~300 offerte al mese e GLM le valuta tutte senza
-- problemi. HR Germania ne fa 2.600: dieci volte tanto, e su quel solo cluster
-- il costo diventa insostenibile.
--
-- Sopra la soglia entra un pre-screening con un modello piccolo, che passa a
-- GLM solo le migliori. È una valvola PER CLUSTER, non una scelta globale:
-- attivarla ovunque pagherebbe un secondo modello anche dove non serve, e
-- aggiungerebbe un anello che può sbagliare là dove GLM ce la fa da solo.
ALTER TABLE clusters
  ADD COLUMN prescreen_threshold integer NOT NULL DEFAULT 800
    CHECK (prescreen_threshold > 0),
  ADD COLUMN prescreen_keep integer NOT NULL DEFAULT 30
    CHECK (prescreen_keep > 0);

COMMENT ON COLUMN clusters.prescreen_threshold IS
  'Offerte al mese oltre le quali entra il pre-screening. Un cluster stretto ne '
  'fa ~300; oltre ~800 il costo di valutare tutto con GLM non regge.';
COMMENT ON COLUMN clusters.prescreen_keep IS
  'Quante offerte il pre-screening passa a GLM.';


-- Volume reale degli ultimi 30 giorni, e se la valvola è attiva.
CREATE VIEW cluster_volume_v AS
SELECT c.id, c.family, c.country,
       count(jc.job_id) FILTER (
         WHERE jc.first_seen_at > now() - interval '30 days') AS offerte_30g,
       c.prescreen_threshold,
       c.prescreen_keep,
       count(jc.job_id) FILTER (
         WHERE jc.first_seen_at > now() - interval '30 days')
         > c.prescreen_threshold AS prescreening_attivo
FROM clusters c
LEFT JOIN job_clusters jc ON jc.cluster_id = c.id
GROUP BY c.id, c.family, c.country, c.prescreen_threshold, c.prescreen_keep;

COMMENT ON VIEW cluster_volume_v IS
  'Volume mensile per cluster e stato della valvola di pre-screening.';


CREATE OR REPLACE FUNCTION cluster_needs_prescreen(p_cluster_id uuid)
RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT COALESCE(prescreening_attivo, false)
    FROM cluster_volume_v WHERE id = p_cluster_id
$$;
