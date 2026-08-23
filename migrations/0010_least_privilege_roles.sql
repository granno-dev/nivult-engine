-- 0010 — Ruoli a privilegio minimo.
--
-- Fino a qui l'applicazione si connetteva con un ruolo che poteva fare
-- qualunque cosa, DROP TABLE compreso. Separati:
--
--   nivult_migrator  DDL. Lo usa solo il runner di migrazioni.
--   nivult_app       solo DML. Lo usa tutto il resto.
--
-- Una SQL injection o un bug nell'applicazione non possono più alterare lo
-- schema né svuotare una tabella con TRUNCATE.
--
-- I ruoli nascono SENZA password e SENZA LOGIN: le credenziali le assegna
-- deploy/setup-roles.sh leggendo da variabile d'ambiente. In una migrazione
-- versionata non entrano segreti.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nivult_app') THEN
    CREATE ROLE nivult_app NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nivult_migrator') THEN
    CREATE ROLE nivult_migrator NOLOGIN;
  END IF;
END $$;

-- In public nessuno crea oggetti tranne il migrator.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT  USAGE          ON SCHEMA public TO nivult_app;
GRANT  USAGE, CREATE  ON SCHEMA public TO nivult_migrator;

DO $$
BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO nivult_app, nivult_migrator',
                 current_database());
END $$;

-- Oggetti già esistenti.
-- Niente TRUNCATE e niente REFERENCES: l'applicazione non deve poter svuotare
-- una tabella in un colpo solo né agganciare vincoli nuovi.
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES    IN SCHEMA public TO nivult_app;
GRANT USAGE, SELECT                  ON ALL SEQUENCES IN SCHEMA public TO nivult_app;
GRANT EXECUTE                        ON ALL FUNCTIONS IN SCHEMA public TO nivult_app;

-- QUESTA È LA PARTE CHE MORDE.
--
-- I GRANT qui sopra valgono solo per gli oggetti che esistono adesso. Senza
-- ALTER DEFAULT PRIVILEGES, la prima tabella creata da una migrazione futura
-- sarebbe invisibile a nivult_app, e l'applicazione si romperebbe in produzione
-- subito dopo un deploy andato "a buon fine" — con un errore di permessi che
-- non assomiglia per niente alla sua causa.
--
-- I default privileges sono legati al ruolo CHE CREA l'oggetto, quindi vanno
-- impostati sia per nivult_migrator sia per il ruolo che oggi esegue le
-- migrazioni: finché il runner gira come superutente, è quello a creare le
-- tabelle.
DO $$
DECLARE
  r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['nivult_migrator', current_user] LOOP
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
      'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO nivult_app', r);
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
      'GRANT USAGE, SELECT ON SEQUENCES TO nivult_app', r);
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
      'GRANT EXECUTE ON FUNCTIONS TO nivult_app', r);
  END LOOP;
END $$;

COMMENT ON SCHEMA public IS
  'nivult_migrator crea, nivult_app legge e scrive. Vedi migrazione 0010.';
