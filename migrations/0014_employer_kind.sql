-- 0014 — Datore diretto o agenzia.
--
-- Molte offerte hanno come datore un'agenzia (Adecco, Manpower, Randstad) e non
-- chi assume davvero. Sono offerte legittime, e c'è chi le vuole e chi no: due
-- preferenze entrambe valide, che diventeranno un filtro utente nel funnel.
--
-- Quindi si etichetta, non si filtra. Buttare dati in ingestione è
-- irreversibile; etichettarli no.

CREATE TABLE employer_kinds (
  kind  text PRIMARY KEY,
  label text NOT NULL,
  note  text NOT NULL
);

INSERT INTO employer_kinds (kind, label, note) VALUES
  ('direct',          'Datore diretto', 'Assume per sé'),
  ('staffing_agency', 'Agenzia',        'Agenzia per il lavoro o di selezione: non è chi assume');


-- La lista sta in tabella e non in codice per un motivo preciso: se fosse in
-- codice, aggiungere un'agenzia domani non riclassificherebbe le offerte già
-- ingerite. Qui basta un INSERT e una chiamata a reclassify_employers().
CREATE TABLE staffing_agency_patterns (
  pattern  text        PRIMARY KEY CHECK (pattern = lower(btrim(pattern))
                                          AND length(pattern) >= 4),
  note     text,
  added_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE staffing_agency_patterns IS
  'Sottostringhe confrontate a confine di parola sul nome del datore, normalizzato. '
  'Minimo 4 caratteri: un pattern corto produrrebbe falsi positivi a valanga.';

INSERT INTO staffing_agency_patterns (pattern, note) VALUES
  ('adecco',          'somministrazione, internazionale'),
  ('manpower',        'somministrazione, internazionale'),
  ('randstad',        'somministrazione, internazionale'),
  ('gi group',        'somministrazione, IT'),
  ('synergie',        'somministrazione, FR/IT'),
  ('proman',          'somministrazione, FR'),
  ('temporis',        'somministrazione, FR'),
  ('supplay',         'somministrazione, FR'),
  ('expectra',        'somministrazione, FR'),
  ('kelly services',  'somministrazione, internazionale'),
  ('hays',            'selezione'),
  ('michael page',    'selezione'),
  ('page personnel',  'selezione'),
  ('robert half',     'selezione'),
  ('robert walters',  'selezione'),
  ('walters people',  'selezione'),
  ('hudson',          'selezione'),
  ('academic work',   'selezione, SE'),
  ('poolia',          'somministrazione, SE'),
  ('bravura',         'somministrazione, SE'),
  ('umana',           'somministrazione, IT'),
  ('openjobmetis',    'somministrazione, IT'),
  ('etjca',           'somministrazione, IT'),
  ('lavorint',        'somministrazione, IT');


-- Normalizzazione del nome: minuscole, punteggiatura a spazi, spazi compattati.
-- IMMUTABLE perché serve dentro un indice e dentro il confronto.
CREATE OR REPLACE FUNCTION normalize_org(p_name text)
RETURNS text
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
  SELECT btrim(regexp_replace(
           regexp_replace(lower(COALESCE(p_name, '')), '[^a-z0-9]+', ' ', 'g'),
           '\s+', ' ', 'g'))
$$;


-- Confronto a CONFINE DI PAROLA (\m ... \M), non semplice contenimento.
-- Senza, "randstad" matcherebbe dentro nomi che lo contengono per caso, e
-- l'etichetta sbagliata su un datore vero è peggio di un'etichetta mancante.
CREATE OR REPLACE FUNCTION classify_employer(p_name text)
RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE WHEN EXISTS (
    SELECT 1 FROM staffing_agency_patterns p
     WHERE normalize_org(p_name) ~ ('\m' || p.pattern || '\M')
  ) THEN 'staffing_agency' ELSE 'direct' END
$$;


ALTER TABLE jobs ADD COLUMN employer_kind text NOT NULL DEFAULT 'direct'
  REFERENCES employer_kinds(kind);
ALTER TABLE jobs ALTER COLUMN employer_kind DROP DEFAULT;

-- Derivato da trigger e non passato dal client: così vale la stessa regola per
-- ogni percorso di scrittura, e non può divergere fra una fonte e l'altra.
CREATE OR REPLACE FUNCTION jobs_set_employer_kind() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.employer_kind := classify_employer(NEW.organization);
  RETURN NEW;
END $$;

CREATE TRIGGER jobs_employer_kind_trg
  BEFORE INSERT OR UPDATE OF organization ON jobs
  FOR EACH ROW EXECUTE FUNCTION jobs_set_employer_kind();

CREATE INDEX jobs_employer_kind_idx ON jobs (employer_kind)
  WHERE status = 'active' AND duplicate_of_job_id IS NULL;


-- Da chiamare dopo ogni modifica alla lista: è il motivo per cui la lista sta
-- in tabella. Ritorna quante righe hanno cambiato etichetta.
CREATE OR REPLACE FUNCTION reclassify_employers()
RETURNS bigint
LANGUAGE plpgsql AS $$
DECLARE
  n bigint;
BEGIN
  UPDATE jobs
     SET employer_kind = classify_employer(organization)
   WHERE employer_kind IS DISTINCT FROM classify_employer(organization);
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

COMMENT ON FUNCTION reclassify_employers() IS
  'Riapplica la lista alle offerte già ingerite. Da chiamare dopo ogni modifica '
  'a staffing_agency_patterns.';
