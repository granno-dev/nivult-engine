-- 0015 — Pattern di agenzia trovati sui dati veri.
--
-- Aggiunte dopo aver classificato 796 datori reali di France Travail e
-- Arbetsförmedlingen. La lista iniziale ne prendeva il 15,7% e lasciava fuori
-- agenzie francesi grandi (CRIT, Adéquat) e tutta la famiglia di nomi che
-- contengono "interim" come parola a sé.
--
-- È esattamente il ciclo per cui la lista sta in tabella: INSERT più
-- reclassify_employers(), senza rilascio e senza toccare le offerte a mano.

INSERT INTO staffing_agency_patterns (pattern, note) VALUES
  ('crit',         'Groupe CRIT, somministrazione FR — 27 occorrenze nel campione'),
  ('adequat',      'Adéquat, somministrazione FR'),
  ('interim',      'parola a sé: CRIT INTERIM, ADEQUAT INTERIM, LEADER INTERIM...'),
  ('interimaires', 'LES INTERIMAIRES PROFESSIONNELS e simili'),
  ('bemanning',    'somministrazione SE: "... Bemanning AB"'),
  ('start people', 'somministrazione FR/BE'),
  ('leader interim', 'somministrazione FR'),
  ('triangle interim', 'somministrazione FR'),
  ('actual leader', 'somministrazione FR — non "actual" da solo, troppo comune')
ON CONFLICT (pattern) DO NOTHING;

-- Riapplica la lista a ciò che è già stato ingerito.
SELECT reclassify_employers();
