-- 0019 — Portata dei cluster e preferenza sulle agenzie.

-- Il rischio sui crediti non è il piano, è un cluster troppo largo.
-- 20.000 offerte al mese sono ~666 al giorno su tutti i cluster, e un cluster
-- stretto (famiglia × paese) ne produce una decina: il margine è ampio.
-- Ma "tutta la Germania" ne fa 3.500 al giorno da solo, e brucia la quota
-- mensile in meno di una settimana.
--
-- Il tetto scende da 1000 a 200: con la regola del cluster stretto è
-- abbondante, e su un cluster mal definito scatta prima di fare danni.
ALTER TABLE clusters ALTER COLUMN daily_credit_cap SET DEFAULT 200;

COMMENT ON COLUMN clusters.daily_credit_cap IS
  'Tarato su un cluster stretto (famiglia × paese, ~10 offerte al giorno). '
  'Un valore molto più alto è il sintomo di un cluster definito male.';


-- Preferenza dell'utente sul tipo di datore.
--
-- C'è chi le agenzie le vuole e chi no, e sono due preferenze entrambe
-- legittime. Il motore si limita a prevederla: l'onboarding e il pannello
-- preferenze li fa l'altro repo.
--
-- Un array di tipi accettati invece di un booleano sulle sole agenzie: così
-- copre anche 'undisclosed', che è la stessa domanda posta su un terzo caso, e
-- resta coerente con work_arrangements ed employment_types.
ALTER TABLE user_clusters
  ADD COLUMN accepted_employer_kinds text[] NOT NULL
    DEFAULT ARRAY['direct', 'staffing_agency', 'undisclosed'];

ALTER TABLE user_clusters
  ADD CONSTRAINT user_clusters_employer_kinds_ck
    CHECK (cardinality(accepted_employer_kinds) > 0);

COMMENT ON COLUMN user_clusters.accepted_employer_kinds IS
  'Tipi di datore che l''utente vuole ricevere. Il default li accetta tutti: '
  'restringere è una scelta esplicita, non un effetto collaterale.';


-- I valori dell'array non possono avere una FK, quindi si validano qui.
-- Senza, un refuso ('agency' invece di 'staffing_agency') filtrerebbe via tutto
-- in silenzio, e sembrerebbe che per quell'utente non ci siano offerte.
CREATE OR REPLACE FUNCTION assert_employer_kinds_valid() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_unknown text[];
BEGIN
  SELECT array_agg(k) INTO v_unknown
    FROM unnest(NEW.accepted_employer_kinds) AS k
   WHERE k NOT IN (SELECT kind FROM employer_kinds);

  IF v_unknown IS NOT NULL THEN
    RAISE EXCEPTION 'tipi di datore sconosciuti: %', array_to_string(v_unknown, ', ')
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER user_clusters_employer_kinds_trg
  BEFORE INSERT OR UPDATE OF accepted_employer_kinds ON user_clusters
  FOR EACH ROW EXECUTE FUNCTION assert_employer_kinds_valid();
