-- 0016 — Datore non dichiarato.
--
-- Il 5% delle offerte francesi non espone entreprise.nom. Finora il client
-- metteva una stringa di ripiego, che sarebbe finita nel digest come se fosse
-- il nome di un'azienda: un valore inventato nel database è peggio di un vuoto.
--
-- Ora organization può essere NULL e il flag lo classifica, con la stessa
-- logica di link_kind: si etichetta, si decide a valle.

ALTER TABLE jobs ALTER COLUMN organization DROP NOT NULL;

-- rank: a parità di punteggio l'ordine nel digest è
-- direct -> staffing_agency -> undisclosed.
ALTER TABLE employer_kinds ADD COLUMN rank smallint;
UPDATE employer_kinds SET rank = CASE kind WHEN 'direct' THEN 1
                                           WHEN 'staffing_agency' THEN 2 END;

INSERT INTO employer_kinds (kind, label, note, rank) VALUES
  ('undisclosed', 'Datore non dichiarato',
   'La fonte non espone il nome. Nel digest si mostra l''etichetta, MAI un nome inventato.', 3);

ALTER TABLE employer_kinds
  ALTER COLUMN rank SET NOT NULL,
  ADD CONSTRAINT employer_kinds_rank_key UNIQUE (rank),
  ADD CONSTRAINT employer_kinds_rank_ck  CHECK (rank > 0);

COMMENT ON COLUMN employer_kinds.rank IS
  'Ordine di preferenza nel digest a parità di punteggio. Più basso = prima.';


-- Un datore assente non è un datore diretto: è una terza cosa.
CREATE OR REPLACE FUNCTION classify_employer(p_name text)
RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN normalize_org(p_name) = '' THEN 'undisclosed'
    WHEN EXISTS (
      SELECT 1 FROM staffing_agency_patterns p
       WHERE normalize_org(p_name) ~ ('\m' || p.pattern || '\M')
    ) THEN 'staffing_agency'
    ELSE 'direct'
  END
$$;

SELECT reclassify_employers();
