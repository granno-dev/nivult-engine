-- 0024 — Arricchimento organizzazione e firma dei filtri spinti in chiamata.

-- Fill-rate misurato su 200 offerte reali in 4 paesi:
--   org_linkedin_headcount                   100%
--   org_linkedin_recruitment_agency_derived  100%
--   org_linkedin_size                         97,5%
--   org_linkedin_industry                     97%
-- Abbastanza alto da poterci filtrare sopra, almeno su questa fonte.
ALTER TABLE jobs
  ADD COLUMN org_size      text,
  ADD COLUMN org_headcount integer CHECK (org_headcount >= 0),
  ADD COLUMN org_industry  text,
  -- La fonte dichiara se il datore è un'agenzia. Copertura piena, contro il
  -- ~25% della nostra lista di pattern: quando c'è, vale più della lista.
  ADD COLUMN employer_agency_declared boolean;

CREATE INDEX jobs_org_size_idx ON jobs (org_size)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL AND org_size IS NOT NULL;

COMMENT ON COLUMN jobs.employer_agency_declared IS
  'Dichiarato dalla fonte (Fantastic: org_linkedin_recruitment_agency_derived). '
  'NULL sulle fonti che non lo espongono.';


-- L'etichetta del datore ora ha tre ingressi, in ordine di autorevolezza.
CREATE OR REPLACE FUNCTION jobs_set_employer_kind() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF normalize_org(NEW.organization) = '' THEN
    NEW.employer_kind := 'undisclosed';
  ELSIF NEW.employer_agency_declared IS TRUE THEN
    -- La fonte lo dichiara: è più affidabile di un confronto sul nome.
    NEW.employer_kind := 'staffing_agency';
  ELSE
    -- Un declared = false NON annulla la nostra lista: la fonte può non
    -- riconoscere un'agenzia locale che noi conosciamo. Le due evidenze si
    -- sommano, non si sostituiscono.
    NEW.employer_kind := classify_employer(NEW.organization);
  END IF;
  RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS jobs_employer_kind_trg ON jobs;
CREATE TRIGGER jobs_employer_kind_trg
  BEFORE INSERT OR UPDATE OF organization, employer_agency_declared ON jobs
  FOR EACH ROW EXECUTE FUNCTION jobs_set_employer_kind();

CREATE OR REPLACE FUNCTION reclassify_employers()
RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
BEGIN
  UPDATE jobs
     SET employer_kind = CASE
           WHEN normalize_org(organization) = '' THEN 'undisclosed'
           WHEN employer_agency_declared IS TRUE THEN 'staffing_agency'
           ELSE classify_employer(organization) END
   WHERE employer_kind IS DISTINCT FROM CASE
           WHEN normalize_org(organization) = '' THEN 'undisclosed'
           WHEN employer_agency_declared IS TRUE THEN 'staffing_agency'
           ELSE classify_employer(organization) END;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;


-- Filtri spinti nella chiamata.
--
-- Filtrare a monte costa meno crediti che scaricare e scartare, ma si può fare
-- SOLO quando tutti gli iscritti a un cluster vogliono la stessa cosa:
-- altrimenti si restringe un corpus condiviso per la preferenza di uno.
--
-- E c'è un secondo effetto, meno ovvio: il corpus diventa dipendente da CHI è
-- iscritto adesso. Se domani si iscrive qualcuno con filtri più larghi, le
-- offerte saltate ieri non tornano indietro da sole. Per questo la firma dei
-- filtri applicati viene registrata: quando cambia, il cluster ha un buco e
-- va riscaricato.
ALTER TABLE clusters ADD COLUMN pushdown_signature text;

COMMENT ON COLUMN clusters.pushdown_signature IS
  'Filtri spinti nella chiamata all''ultimo scarico. Se cambia, il corpus '
  'raccolto prima è più stretto di quello che serve ora: c''è un buco.';
