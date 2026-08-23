-- 0025 — Vocabolari dei filtri, verificati sul campo.
--
-- Perché in tabella e non in costanti nel codice: l'onboarding costruisce i
-- menu leggendo da qui, e non può proporre all'utente un valore che l'API non
-- riconosce. Stessa ragione della lista delle agenzie.
--
-- LA TRAPPOLA CHE QUESTA TABELLA ESISTE PER CHIUDERE:
-- un valore sbagliato NON dà errore, dà zero risultati — e sembra che il
-- mercato sia vuoto invece che la query sbagliata. Su organization_size il
-- campo di risposta vale "2-10 employees" ma il filtro vuole "2-10": usando il
-- primo la copertura in Italia risultava dello 0,6% invece del 98,8%.
-- Per questo api_value e response_value sono due colonne distinte.

CREATE TABLE filter_values (
  parameter       text     NOT NULL,
  api_value       text     NOT NULL,
  -- Come compare nel campo di risposta, quando è diverso dal valore da mandare.
  response_value  text,
  label           text     NOT NULL,
  sort_order       smallint NOT NULL,
  -- 'verified' = visto in una chiamata vera. 'docs_only' = solo letto nella
  -- documentazione, che negli ultimi giorni ha sbagliato su tre fonti su tre.
  evidence        text     NOT NULL CHECK (evidence IN ('verified','docs_only')),
  verified_at     timestamptz,

  PRIMARY KEY (parameter, api_value),
  CONSTRAINT filter_values_evidence_ck
    CHECK (evidence <> 'verified' OR verified_at IS NOT NULL)
);

CREATE INDEX filter_values_param_idx ON filter_values (parameter, sort_order);

COMMENT ON COLUMN filter_values.api_value IS
  'Valore da mandare nella chiamata. NON sempre uguale a quello che torna nel '
  'campo: vedi organization_size.';
COMMENT ON COLUMN filter_values.response_value IS
  'Valore che compare nel campo di risposta, se diverso da api_value.';

INSERT INTO filter_values (parameter, api_value, response_value, label, sort_order, evidence, verified_at) VALUES
  -- Osservati su 300 offerte reali (IT/DE/FR), fill-rate 100%.
  ('ai_work_arrangement','On-site',NULL,'In sede',1,'verified',now()),
  ('ai_work_arrangement','Hybrid',NULL,'Ibrido',2,'verified',now()),
  ('ai_work_arrangement','Remote OK',NULL,'Remoto possibile',3,'verified',now()),
  ('ai_work_arrangement','Remote Solely',NULL,'Solo da remoto',4,'verified',now()),

  ('ai_experience_level','0-2',NULL,'Fino a 2 anni',1,'verified',now()),
  ('ai_experience_level','2-5',NULL,'2-5 anni',2,'verified',now()),
  ('ai_experience_level','5-10',NULL,'5-10 anni',3,'verified',now()),
  ('ai_experience_level','10+',NULL,'Oltre 10 anni',4,'verified',now()),

  ('ai_employment_type','FULL_TIME',NULL,'Tempo pieno',1,'verified',now()),
  ('ai_employment_type','PART_TIME',NULL,'Part time',2,'verified',now()),
  ('ai_employment_type','CONTRACTOR',NULL,'Collaborazione',3,'verified',now()),
  ('ai_employment_type','TEMPORARY',NULL,'Temporaneo',4,'verified',now()),
  ('ai_employment_type','INTERN',NULL,'Tirocinio',5,'verified',now()),
  ('ai_employment_type','OTHER',NULL,'Altro',6,'verified',now()),
  -- Elencati nella documentazione ma MAI osservati nel campione.
  ('ai_employment_type','VOLUNTEER',NULL,'Volontariato',7,'docs_only',NULL),
  ('ai_employment_type','PER_DIEM',NULL,'A giornata',8,'docs_only',NULL),

  ('ai_education','high school',NULL,'Diploma',1,'verified',now()),
  ('ai_education','associate degree',NULL,'Laurea breve',2,'verified',now()),
  ('ai_education','bachelor degree',NULL,'Laurea triennale',3,'verified',now()),
  ('ai_education','professional certificate',NULL,'Certificazione professionale',4,'verified',now()),
  ('ai_education','postgraduate degree',NULL,'Laurea magistrale o superiore',5,'verified',now()),

  -- QUI il formato del filtro differisce dal campo di risposta.
  ('organization_size','1','1 employee','1 persona',1,'verified',now()),
  ('organization_size','2-10','2-10 employees','2-10',2,'verified',now()),
  ('organization_size','11-50','11-50 employees','11-50',3,'verified',now()),
  ('organization_size','51-200','51-200 employees','51-200',4,'verified',now()),
  ('organization_size','201-500','201-500 employees','201-500',5,'verified',now()),
  ('organization_size','501-1000','501-1,000 employees','501-1.000',6,'verified',now()),
  ('organization_size','1001-5000','1,001-5,000 employees','1.001-5.000',7,'verified',now()),
  ('organization_size','5001-10000','5,001-10,000 employees','5.001-10.000',8,'verified',now()),
  ('organization_size','10001+','10,001+ employees','Oltre 10.000',9,'verified',now()),

  -- ai_language vuole il NOME della lingua, non il codice ISO: 'de' dà zero
  -- risultati senza errore. Lista parziale: solo ciò che il campione conteneva.
  ('ai_language','English',NULL,'Inglese',1,'verified',now()),
  ('ai_language','German',NULL,'Tedesco',2,'verified',now()),
  ('ai_language','French',NULL,'Francese',3,'verified',now()),
  ('ai_language','Italian',NULL,'Italiano',4,'verified',now()),

  ('organization_agency','exclude',NULL,'Escludi le agenzie',1,'verified',now()),
  ('organization_agency','only',NULL,'Solo agenzie',2,'docs_only',NULL),

  ('ai_visa_sponsorship','only',NULL,'Solo con sponsorship visto',1,'verified',now()),
  ('ai_visa_sponsorship','exclude',NULL,'Senza sponsorship visto',2,'docs_only',NULL);


-- Quali colonne di user_clusters devono pescare da quale vocabolario.
CREATE TABLE filter_bindings (
  column_name text PRIMARY KEY,
  parameter   text NOT NULL,
  note        text
);

INSERT INTO filter_bindings (column_name, parameter, note) VALUES
  ('work_arrangements','ai_work_arrangement',NULL),
  ('employment_types','ai_employment_type',NULL),
  ('languages','ai_language','nome della lingua, NON il codice ISO'),
  ('company_sizes','organization_size','usare api_value, non response_value');
