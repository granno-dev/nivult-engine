-- 0068: il ruolo di sola lettura per l'API clienti (il "vivo")
--
-- Nato il 03/10/2026: le liste paginate dell'API v1 si servono da
-- Postgres (indice + chiave primaria), non piu' dal DuckDB sul volume
-- di rete (47,8 secondi a pagina, misurato). La regola «l'API non
-- tocca il db di produzione» diventa: l'API tocca il db SOLO con
-- questo ruolo — SELECT su cinque tabelle, timeout duro, poche
-- connessioni. Un attaccante con questa password legge offerte
-- pubbliche e basta: non scrive, non cancella, non vede utenti.

CREATE ROLE nivult_api_lettura LOGIN PASSWORD '__PASSWORD__';

GRANT CONNECT ON DATABASE nivult_ats TO nivult_api_lettura;
GRANT USAGE ON SCHEMA public TO nivult_api_lettura;
GRANT SELECT ON ats_jobs, ats_companies, job_classifications,
                tecnologie_v1, sintesi_finali, offerte_dettagli,
                stipendi_benchmark
  TO nivult_api_lettura;

-- cinture: nessuna query del ruolo puo' vivere oltre 8 secondi,
-- e non puo' avere piu' di 6 connessioni aperte insieme
ALTER ROLE nivult_api_lettura SET statement_timeout = '8s';
ALTER ROLE nivult_api_lettura CONNECTION LIMIT 6;
