SET lock_timeout = '2s';

DROP TRIGGER IF EXISTS trg_opensearch_azienda ON ats_companies;

CREATE TRIGGER trg_opensearch_azienda_insert_delete
  AFTER INSERT OR DELETE ON ats_companies
  FOR EACH ROW EXECUTE FUNCTION ats_jobs_search_azienda();

CREATE TRIGGER trg_opensearch_azienda_update
  AFTER UPDATE OF company_name ON ats_companies
  FOR EACH ROW
  WHEN (OLD.company_name IS DISTINCT FROM NEW.company_name)
  EXECUTE FUNCTION ats_jobs_search_azienda();
