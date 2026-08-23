-- 0028 — Logo dell'azienda: quale fonte, e come si serve.
--
-- Fill-rate misurato su 300 offerte reali in IT/DE/FR:
--   org_logo_permalink   83%   (92% IT, 76% DE, 82% FR)
--   organization_logo    49%   (l'avevamo stimato al 40%)
--   almeno uno dei due   94%
--   domain_derived       98%   -> il ripiego su Logo.dev copre quasi tutto
--
-- org_logo_permalink diventa quindi la fonte principale.

ALTER TABLE jobs ADD COLUMN org_logo_permalink text;

COMMENT ON COLUMN jobs.org_logo_permalink IS
  'Logo principale (83% di copertura). Vedi organization_logo per il ripiego.';
COMMENT ON COLUMN jobs.organization_logo IS
  'Secondo anello della catena dei loghi (49%). Il primo è org_logo_permalink.';
