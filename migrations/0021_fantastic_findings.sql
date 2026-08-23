-- 0021 — Quello che il probe di Fantastic ha confermato e smentito.

-- CONFERMATO: la scala di ai_experience_level è quella che avevamo ipotizzato.
-- Un'offerta reale riporta '5-10'. Il vocabolario non è più provvisorio.
COMMENT ON TABLE experience_levels IS
  'Vocabolario di ai_experience_level, CONFERMATO sui dati reali di '
  'Fantastic.jobs il 2026-08-23 (valore osservato: 5-10).';

-- SMENTITO: org_linkedin_size non esiste nella risposta di active-ats, nemmeno
-- con Basic Organization Enrichment attivo. I campi di organizzazione presenti
-- sono org_linkedin_slug, organization_url e organization_logo: nessuna
-- dimensione, nessun numero di dipendenti, nessun settore.
--
-- Il filtro sulla dimensione azienda resta quindi NON applicabile, su nessuna
-- fonte. France Travail ha trancheEffectifEtab ma da solo non basta: un filtro
-- che funziona su una fonte e non sulle altre produce risultati che dipendono
-- da dove è passata l'offerta, non da cosa cerca l'utente.
COMMENT ON COLUMN user_clusters.company_sizes IS
  'NON APPLICABILE. Verificato il 2026-08-23: Fantastic non espone la '
  'dimensione azienda in active-ats nemmeno con Organization Enrichment. '
  'France Travail ha trancheEffectifEtab, ma un filtro attivo su una sola '
  'fonte darebbe risultati che dipendono dalla provenienza dell''offerta.';
