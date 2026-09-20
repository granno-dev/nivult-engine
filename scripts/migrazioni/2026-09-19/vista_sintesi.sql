-- Da dove si legge LA sintesi di un'offerta, adesso che i modelli sono due.
--
-- Ordine di precedenza:
--   1. se il 2B ha RIPASSATO la sintesi di mT5, vale la sua — ed e' proprio il caso
--      in cui mT5 aveva esitato, quindi e' il ripasso che stavamo cercando;
--   2. se esiste una sintesi in estrazioni_v2b, e' del 2B in modalita' piena
--      (com'era fino al 19/09/2026);
--   3. altrimenti quella di mT5.
--
-- `da` dice quale dei due l'ha scritta, cosi' non serve indovinarlo: se un giorno
-- vorremo riscrivere tutte quelle di mT5 con un modello migliore, sapremo quali.
ALTER TABLE sintesi_mt5 ADD COLUMN IF NOT EXISTS sintesi_originale text;

CREATE OR REPLACE VIEW sintesi_finali AS
SELECT j.id AS job_id,
       coalesce(CASE WHEN m.ripassata_at IS NOT NULL THEN m.sintesi END,
                e.sintesi, m.sintesi)                  AS sintesi,
       CASE WHEN m.ripassata_at IS NOT NULL AND m.sintesi IS NOT NULL THEN 'nivult-2b+ripasso'
            WHEN e.sintesi IS NOT NULL                              THEN 'nivult-2b'
            WHEN m.sintesi IS NOT NULL                              THEN 'nivult-mt5'
       END                                             AS da,
       m.fiducia                                       AS fiducia_mt5,
       m.ripassata_at                                  AS ripassata_at,
       m.sintesi_originale                             AS sintesi_mt5_originale
  FROM ats_jobs j
  LEFT JOIN estrazioni_v2b e ON e.job_id = j.id
  LEFT JOIN sintesi_mt5   m ON m.job_id = j.id
 WHERE coalesce(e.sintesi, m.sintesi) IS NOT NULL;

GRANT SELECT ON sintesi_finali TO nivult_operaio;
