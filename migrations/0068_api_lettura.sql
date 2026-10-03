-- 0068: il ruolo di sola lettura per l'API clienti (il "vivo")
--
-- Nato il 03/10/2026: le liste paginate dell'API v1 si servono da
-- Postgres (indice + chiave primaria), non piu' dal DuckDB sul volume
-- di rete (47,8 secondi a pagina, misurato). La regola «l'API non
-- tocca il db di produzione» diventa: l'API tocca il db SOLO con
-- questo ruolo — SELECT su sette tabelle, timeout duro, poche
-- connessioni. Un attaccante con questa password legge offerte
-- pubbliche e basta: non scrive, non cancella, non vede utenti.
--
-- Come i ruoli della 0010: nasce SENZA password e SENZA LOGIN — la
-- credenziale la assegna deploy/setup-roles.sh dall'ambiente
-- (API_LETTURA_PASSWORD). In una migrazione versionata non entrano
-- segreti.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles
                 WHERE rolname = 'nivult_api_lettura') THEN
    CREATE ROLE nivult_api_lettura NOLOGIN;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO nivult_api_lettura;

-- i GRANT sono condizionali: in CI le migrazioni girano da zero e le
-- tabelle ATS potrebbero non esserci ancora (lo schema del corpus vive
-- in src/nivult/ats/schema.sql, non nella catena). In produzione ci
-- sono tutte. Chi manca oggi la prende al prossimo giro di ruoli.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['ats_jobs', 'ats_companies',
                           'job_classifications', 'tecnologie_v1',
                           'sintesi_finali', 'offerte_dettagli',
                           'stipendi_benchmark']
  LOOP
    IF EXISTS (SELECT 1 FROM pg_tables
               WHERE schemaname = 'public' AND tablename = t) THEN
      EXECUTE format('GRANT SELECT ON %I TO nivult_api_lettura', t);
    END IF;
  END LOOP;
END $$;

-- cinture: nessuna query del ruolo puo' vivere oltre 8 secondi,
-- e non puo' avere piu' di 6 connessioni aperte insieme
ALTER ROLE nivult_api_lettura SET statement_timeout = '8s';
ALTER ROLE nivult_api_lettura CONNECTION LIMIT 6;
