-- 0075: il deep link nativo e' configurazione del prodotto, non schema.
--
-- La 0073 proteggeva la coppia state/challenge ma fissava anche il callback di
-- Nivult. Con una seconda API (Mavult) quel vincolo impediva il suo callback:
-- la destinazione resta validata dall'applicazione, il database controlla solo
-- che i campi nativi arrivino entrambi e nella forma attesa.
ALTER TABLE oauth_flows DROP CONSTRAINT oauth_flows_native_ck;

ALTER TABLE oauth_flows ADD CONSTRAINT oauth_flows_native_ck CHECK (
  (native_state IS NULL AND native_challenge IS NULL)
  OR (
    destinazione IS NOT NULL
    AND native_state IS NOT NULL
    AND length(native_state) BETWEEN 1 AND 512
    AND native_challenge IS NOT NULL
    AND native_challenge ~ '^[A-Za-z0-9_-]{43}$'
  )
);
