-- 0051 — `ats` entra nel vocabolario delle fonti.
--
-- Il sistema ATS legge le pagine carriere delle aziende e vive in un
-- database suo (`nivult_ats`), che qui non si tocca. `nivult.ponte_ats`
-- ne travasa le offerte nel funnel riusando `upsert_job`: dal punto di
-- vista del motore l'ATS è una fonte come le altre, e deve poter comparire
-- dove le fonti compaiono.
--
-- Tre vocabolari si aprono, due no — e la differenza non è una svista:
--
--   · `jobs.source` e `ingestion_runs.source` si aprono, perché le offerte
--     ATS sono righe di `jobs` e ogni travaso è un giro di ingestione con
--     un suo run;
--   · `api_usage.provider` si apre, perché la regola di casa vuole in
--     `api_usage` anche le chiamate gratuite: non per i soldi, ma per
--     vedere una fonte che degrada prima che diventi un problema. Un
--     ponte che smettesse di portare offerte sarebbe altrimenti muto;
--   · `cluster_source_queries` e `cluster_source_cursors` NON si aprono.
--     Sono per le fonti che vanno interrogate con termini di ricerca e
--     paginate. L'ATS non è nessuna delle due: la famiglia professionale
--     gliel'ha già assegnata la sua classificazione, con lo stesso
--     vocabolario di `job_families`, e la lettura è una query locale che
--     vede tutto in un colpo. Aggiungerlo lì creerebbe due tabelle di
--     configurazione che nessuno riempirà mai, e `cluster_coverage_v`
--     segnalerebbe per sempre cluster «scoperti» da una fonte che non ha
--     bisogno di essere coperta.
--
-- La quota è a zero, cioè fonte gratuita: contabilizzata, mai bloccante.
-- Costa davvero zero — il database è in casa, sulla stessa macchina.

ALTER TABLE jobs DROP CONSTRAINT jobs_source_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_source_check CHECK (
    source = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                        'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                        'ats'])
);

ALTER TABLE ingestion_runs DROP CONSTRAINT ingestion_runs_source_check;
ALTER TABLE ingestion_runs ADD CONSTRAINT ingestion_runs_source_check CHECK (
    source = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                        'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                        'ats'])
);

ALTER TABLE api_usage DROP CONSTRAINT api_usage_provider_check;
ALTER TABLE api_usage ADD CONSTRAINT api_usage_provider_check CHECK (
    provider = ANY (ARRAY['fantastic', 'france_travail', 'bundesagentur',
                          'arbetsformedlingen', 'nav', 'tyomarkkinatori',
                          'glm', 'bge-m3', 'bge-reranker', 'ats'])
);

INSERT INTO provider_quotas (provider, monthly_credits_cap, monthly_requests_cap, note)
VALUES ('ats', 0, 0, 'gratuita: database locale, nessuna chiamata esterna')
ON CONFLICT (provider) DO NOTHING;
