SET lock_timeout = '2s';

DROP TRIGGER IF EXISTS trg_delta_azienda ON ats_companies;

CREATE TRIGGER trg_delta_azienda_delete
  AFTER DELETE ON ats_companies
  FOR EACH ROW EXECUTE FUNCTION ats_tocca_azienda_collegata();

CREATE TRIGGER trg_delta_azienda_update
  AFTER UPDATE OF company_name ON ats_companies
  FOR EACH ROW
  WHEN (OLD.company_name IS DISTINCT FROM NEW.company_name)
  EXECUTE FUNCTION ats_tocca_azienda_collegata();
