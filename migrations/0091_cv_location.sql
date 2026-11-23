-- 0091 — Località dichiarata dal CV e disponibilità al trasferimento.
-- Valori separati: una città o una disponibilità non dichiarata resta NULL.

ALTER TABLE user_cvs
  ADD COLUMN location_city text,
  ADD COLUMN location_country char(2),
  ADD COLUMN relocation_open boolean;

ALTER TABLE user_cvs ADD CONSTRAINT user_cvs_location_city_ck
  CHECK (location_city IS NULL OR char_length(location_city) BETWEEN 1 AND 120);
ALTER TABLE user_cvs ADD CONSTRAINT user_cvs_location_country_ck
  CHECK (location_country IS NULL OR location_country ~ '^[A-Z]{2}$');
