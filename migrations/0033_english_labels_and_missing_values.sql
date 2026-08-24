-- 0033 — Etichette leggibili in inglese, e i valori che mancavano.
--
-- Le etichette c'erano già, ma in italiano: la lingua interna del motore,
-- non quella del prodotto. Il sito è in inglese, l'API restituiva il valore
-- grezzo, e all'utente arrivava `FULL_TIME` e `staffing_agency`. La colonna
-- `label` diventa quello che il sito mostra davvero, quindi va scritta nella
-- lingua base del prodotto — la traduzione, quando servirà, è un lavoro del
-- sito sulla chiave stabile `api_value`.

-- ── 1. Tipi di contratto ───────────────────────────────────────────────────
UPDATE filter_values SET label = 'Full time'     WHERE parameter='ai_employment_type' AND api_value='FULL_TIME';
UPDATE filter_values SET label = 'Part time'     WHERE parameter='ai_employment_type' AND api_value='PART_TIME';
UPDATE filter_values SET label = 'Contract'      WHERE parameter='ai_employment_type' AND api_value='CONTRACTOR';
UPDATE filter_values SET label = 'Temporary'     WHERE parameter='ai_employment_type' AND api_value='TEMPORARY';
UPDATE filter_values SET label = 'Internship'    WHERE parameter='ai_employment_type' AND api_value='INTERN';
UPDATE filter_values SET label = 'Per diem'      WHERE parameter='ai_employment_type' AND api_value='PER_DIEM';
UPDATE filter_values SET label = 'Volunteer'     WHERE parameter='ai_employment_type' AND api_value='VOLUNTEER';
UPDATE filter_values SET label = 'Other'         WHERE parameter='ai_employment_type' AND api_value='OTHER';

-- ── 2. Lingue ──────────────────────────────────────────────────────────────
UPDATE filter_values SET label = api_value WHERE parameter = 'ai_language';

-- Lo svedese mancava dal vocabolario mentre 131 offerte reali erano già
-- ingerite in quella lingua: chi cerca in Svezia non poteva selezionarla.
--
-- Si aggiunge SOLO ciò che il corpus contiene davvero. Riempire l'elenco con
-- le lingue che ci piacerebbe coprire darebbe all'utente filtri che non
-- trovano niente, e un digest vuoto letto come "non c'è lavoro per me"
-- invece che come "quella lingua non la leggiamo ancora".
INSERT INTO filter_values (parameter, api_value, response_value, label, sort_order,
                           evidence, verified_at)
VALUES
  ('ai_language', 'Swedish',    'Swedish',    'Swedish',    5, 'verified', now()),
  ('ai_language', 'Portuguese', 'Portuguese', 'Portuguese', 6, 'verified', now())
ON CONFLICT DO NOTHING;

-- ── 3. Modalità di lavoro ──────────────────────────────────────────────────
UPDATE filter_values SET label = api_value
  WHERE parameter = 'ai_work_arrangement' AND (label IS NULL OR label = '');

-- ── 4. Sponsorship del visto ───────────────────────────────────────────────
--
-- CLAUDE.md lo elenca fra i filtri PROMESSI all'utente, con campo pieno al
-- 100%, e non era mai arrivato nell'interfaccia. Le etichette dicono cosa
-- succede all'utente, non come è fatto il campo.
UPDATE filter_values SET label = 'Only jobs that sponsor a visa'
  WHERE parameter='ai_visa_sponsorship' AND api_value='only';
UPDATE filter_values SET label = 'Exclude jobs that require sponsorship'
  WHERE parameter='ai_visa_sponsorship' AND api_value='exclude';

-- ── 5. Agenzie ─────────────────────────────────────────────────────────────
UPDATE filter_values SET label = 'Exclude agencies'
  WHERE parameter='organization_agency' AND api_value='exclude';
UPDATE filter_values SET label = 'Agencies only'
  WHERE parameter='organization_agency' AND api_value='only';

-- ── 6. Chi assume ──────────────────────────────────────────────────────────
--
-- `employer_kinds` non aveva affatto una colonna di etichetta, quindi il
-- sito mostrava la chiave: `staffing_agency`, `undisclosed`. Sono i tre
-- valori che l'utente sceglie nel funnel, e vanno detti in parole sue.
ALTER TABLE employer_kinds ADD COLUMN IF NOT EXISTS label text;

UPDATE employer_kinds SET label = 'Direct employers'     WHERE kind = 'direct';
UPDATE employer_kinds SET label = 'Recruitment agencies' WHERE kind = 'staffing_agency';
UPDATE employer_kinds SET label = 'Employer not named'   WHERE kind = 'undisclosed';

-- Da qui in poi un tipo nuovo senza etichetta è un errore, non una svista da
-- scoprire guardando l'interfaccia.
ALTER TABLE employer_kinds ALTER COLUMN label SET NOT NULL;

COMMENT ON COLUMN employer_kinds.label IS
  'Come si chiama per l''utente. La chiave resta `kind`, che e'' quella su cui '
  'si filtra: l''etichetta si puo'' riscrivere senza toccare i dati.';
