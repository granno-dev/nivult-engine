-- 0011 — Proprietà degli oggetti a nivult_migrator.
--
-- Senza questa migrazione nivult_migrator sarebbe decorativo: può creare
-- oggetti nuovi (ha CREATE su public), ma NON può fare ALTER o DROP su quelli
-- che esistono già, perché in Postgres quelle operazioni le può fare solo il
-- proprietario. La prima migrazione futura che aggiunge una colonna a una
-- tabella esistente fallirebbe.
--
-- Le estensioni restano al superutente: i loro oggetti non possono appartenere
-- a un ruolo non privilegiato, e comunque non li tocchiamo mai.

DO $$
DECLARE
  obj record;
BEGIN
  -- Tabelle, sequenze e viste che non appartengono a un'estensione.
  FOR obj IN
    SELECT c.relname,
           CASE c.relkind WHEN 'r' THEN 'TABLE'
                          WHEN 'S' THEN 'SEQUENCE'
                          WHEN 'v' THEN 'VIEW' END AS kind
      FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public'
       AND c.relkind IN ('r','S','v')
       -- Niente oggetti di estensione: non possono appartenere a un ruolo
       -- non privilegiato, e non li tocchiamo mai.
       AND NOT EXISTS (
             SELECT 1 FROM pg_depend d
              WHERE d.objid = c.oid AND d.deptype = 'e')
       -- Niente sequenze legate a una colonna (bigserial/identity): Postgres
       -- le fa seguire automaticamente il proprietario della tabella, e un
       -- ALTER SEQUENCE diretto viene rifiutato. Che al primo tentativo sia
       -- passato era solo l'ordine degli oggetti: la tabella capitava prima
       -- della sua sequenza, quindi l'ALTER successivo era un non-fare.
       AND NOT EXISTS (
             SELECT 1 FROM pg_depend d
              WHERE d.objid = c.oid AND d.deptype = 'a'
                AND d.refclassid = 'pg_class'::regclass)
  LOOP
    EXECUTE format('ALTER %s public.%I OWNER TO nivult_migrator', obj.kind, obj.relname);
  END LOOP;

  -- Funzioni del motore.
  FOR obj IN
    SELECT p.oid::regprocedure AS sig
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'public'
       AND NOT EXISTS (
             SELECT 1 FROM pg_depend d
              WHERE d.objid = p.oid AND d.deptype = 'e')
  LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO nivult_migrator', obj.sig);
  END LOOP;
END $$;

-- Lo schema stesso: serve a nivult_migrator per poterci creare dentro senza
-- dipendere da un GRANT che qualcuno potrebbe revocare.
ALTER SCHEMA public OWNER TO nivult_migrator;
