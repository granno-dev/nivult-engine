-- 0066 — I crediti comprati sono una RICARICA, non un abbonamento
-- (28/09/2026).
--
-- Prima il webhook Creem alzava crediti_mensili: chi pagava una volta
-- si trovava il volume alzato OGNI mese per sempre — un abbonamento
-- regalato. Ora i crediti comprati vivono in users.crediti_extra: non
-- scadono, non si azzerano al primo del mese, e si spendono solo
-- quando la franchigia gratuita del mese e' finita. L'ordine di spesa
-- (prima il gratis, poi il pagato) sta in chiavi.py.

ALTER TABLE users ADD COLUMN crediti_extra integer NOT NULL DEFAULT 0;

ALTER TABLE users ADD CONSTRAINT users_crediti_extra_ck
  CHECK (crediti_extra >= 0);
