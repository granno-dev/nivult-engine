-- 0037 — Quante ricerche vale un piano, imposto dal database.
--
-- Il sito promette 2 aree di ricerca su Basic, 6 su Pro, 12 su Ultra. L'API
-- non lo verificava: si poteva iscriversi a quanti cluster si voleva.
--
-- Finora era solo una promessa non mantenuta. Diventa un problema di soldi
-- adesso che l'utente può APRIRE un cluster che non esiste: ogni cluster
-- nuovo entra nell'ingestione notturna e consuma crediti dal tetto mensile
-- di Fantastic — 20.000 offerte, condivise fra tutti — e continua a
-- consumarli finché resta attivo. Un utente non deve poter aprire da solo
-- più mercati di quanti ne paga.
ALTER TABLE plan_quotas
  ADD COLUMN max_searches integer NOT NULL DEFAULT 2 CHECK (max_searches > 0);

-- Gli stessi numeri della tabella prezzi sul sito. Se un giorno divergono,
-- diverge il prodotto: qui è la fonte, il sito la racconta.
UPDATE plan_quotas SET max_searches =  2 WHERE plan = 'basic';
UPDATE plan_quotas SET max_searches =  6 WHERE plan = 'pro';
UPDATE plan_quotas SET max_searches = 12 WHERE plan = 'ultra';

ALTER TABLE plan_quotas ALTER COLUMN max_searches DROP DEFAULT;

COMMENT ON COLUMN plan_quotas.max_searches IS
  'Quante ricerche attive puo'' tenere un piano. E'' anche il freno sui '
  'crediti: ogni ricerca e'' un cluster che consuma il tetto mensile della '
  'fonte finche'' resta attivo.';


-- ── Il cluster si apre a richiesta ─────────────────────────────────────────
--
-- Un cluster nasceva a mano. Ma il modello per cluster è un meccanismo di
-- CONDIVISIONE dell'ingestione — dieci utenti sullo stesso mercato lo
-- scaricano una volta — non un catalogo di ciò che l'utente può chiedere.
-- Mostrare solo i cluster già attivi significava mostrare all'utente il
-- nostro stato interno al posto del prodotto.
--
-- La funzione è qui e non nell'API perché la corsa fra due utenti che
-- aprono lo stesso mercato nello stesso istante si risolve dove sta il
-- vincolo di unicità, non a valle.
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
    RETURN v_id;
  END IF;

  INSERT INTO clusters (family, country) VALUES (p_family, p_country)
  ON CONFLICT (family, country) DO UPDATE SET updated_at = now()
  RETURNING id INTO v_id;

  RETURN v_id;
END;
$$;

COMMENT ON FUNCTION apri_cluster IS
  'Il cluster per questa coppia famiglia x paese, creandolo se non c''e''. '
  'I tetti di credito prendono i default della tabella: un cluster stretto '
  'ne consuma una decina al giorno, e daily_credit_cap scatta prima dei '
  'danni su uno definito male.';
