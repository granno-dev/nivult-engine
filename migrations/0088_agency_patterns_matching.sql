-- 0088 — Agenzie emerse dal banco manuale del matching.
--
-- Sono nomi verificati di societa' di selezione o la qualifica esplicita nel
-- nome del datore. Niente termini generici come consulting, HR o executive:
-- un falso «staffing_agency» escluderebbe un datore diretto da una ricerca.

INSERT INTO staffing_agency_patterns (pattern, note) VALUES
  ('areajob',               'Areajob S.p.A., agenzia per il lavoro'),
  ('ali spa',               'Ali S.p.A., somministrazione e selezione'),
  ('apisjob',               'Apis S.p.A., ricerca e selezione'),
  ('aims international',    'Aims International, executive search'),
  ('barrington james',      'Barrington James, recruitment ed executive search'),
  ('adhr',                  'ADHR Group, agenzia per il lavoro'),
  ('agenzia per il lavoro', 'Qualifica esplicita nel nome del datore'),
  ('agenzia di selezione',  'Qualifica esplicita nel nome del datore')
ON CONFLICT (pattern) DO NOTHING;

SELECT reclassify_employers();
