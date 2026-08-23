-- 0022 — Famiglie professionali, e la correzione di due conclusioni sbagliate.
--
-- CORREZIONE 1: org_linkedin_size ESISTE.
-- Nella 0021 avevo scritto che Fantastic non espone la dimensione azienda.
-- Era sbagliato: l'arricchimento è OPT-IN, serve
-- include_basic_organization_details=true. Senza il flag la risposta ha 49
-- campi, con il flag 69, fra cui org_linkedin_size, org_linkedin_headcount,
-- org_linkedin_industry e org_linkedin_recruitment_agency_derived.
-- Avevo concluso "non c'è" da un'assenza che era una mia omissione.
--
-- CORREZIONE 2: il filtro per tassonomia esiste.
-- ai_taxonomies_a è un PARAMETRO di ricerca, non solo un campo di risposta.
-- Cambia cosa può essere un cluster: si chiede la famiglia professionale
-- invece di rincorrere i sinonimi del titolo in ogni lingua.
--   Germania, 7 giorni:  title='Human Resources'      ->    26 offerte
--                        ai_taxonomies_a='Human Resources' -> 646 offerte

CREATE TABLE job_families (
  family     text PRIMARY KEY,
  sort_order smallint NOT NULL,
  note       text
);

COMMENT ON TABLE job_families IS
  'Valori di ai_taxonomies_a. È la tassonomia del fornitore, non una nostra: '
  'diventa il menu delle famiglie in onboarding e l''identità di un cluster. '
  'Verificati 33 valori il 2026-08-23; la documentazione ne lascia intendere '
  'qualcuno in più, quindi la lista è un minimo, non una certezza.';

INSERT INTO job_families (family, sort_order) VALUES
  ('Administrative', 1), ('Agriculture', 2), ('Art & Design', 3),
  ('Construction', 4), ('Consulting', 5), ('Creative & Media', 6),
  ('Customer Service & Support', 7), ('Data & Analytics', 8), ('Education', 9),
  ('Energy', 10), ('Engineering', 11), ('Environmental & Sustainability', 12),
  ('Finance & Accounting', 13), ('Food & Beverage', 14),
  ('Government & Public Sector', 15), ('Healthcare', 16), ('Hospitality', 17),
  ('Human Resources', 18), ('Legal', 19), ('Logistics', 20),
  ('Management & Leadership', 21), ('Manufacturing', 22), ('Marketing', 23),
  ('Retail', 24), ('Sales', 25), ('Science & Research', 26),
  ('Security & Safety', 27), ('Social Services', 28), ('Software', 29),
  ('Sports & Recreation', 30), ('Technology', 31), ('Trades', 32),
  ('Transportation', 33);


-- Le conclusioni della 0021 vanno riscritte, non lasciate lì.
COMMENT ON COLUMN user_clusters.company_sizes IS
  'RIAPRIBILE su Fantastic: org_linkedin_size e org_linkedin_headcount arrivano '
  'con include_basic_organization_details=true, e organization_size è anche un '
  'parametro di ricerca. Resta però assente sulle fonti nazionali, quindi un '
  'filtro attivo darebbe risultati che dipendono dalla provenienza dell''offerta. '
  'Decisione di prodotto, non più un limite tecnico.';

COMMENT ON TABLE experience_levels IS
  'Vocabolario di ai_experience_level, confermato sui dati reali il 2026-08-23. '
  'È anche un parametro di ricerca di Fantastic: si può filtrare in chiamata.';
