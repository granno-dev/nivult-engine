-- nivult:no-transaction
-- 0001 — Estensioni, impostazioni di database, funzioni condivise.
--
-- Marcata no-transaction perché ALTER DATABASE ... SET non è sempre gradito
-- dentro un blocco transazionale. Ogni istruzione qui è idempotente, quindi
-- una riapplicazione parziale non fa danni.

CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;

-- Tutto il motore ragiona in UTC. Serve soprattutto al circuit breaker:
-- current_date deve indicare lo stesso giorno per ogni worker, comunque sia
-- configurato il client che si connette.
DO $$
BEGIN
  EXECUTE format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'UTC');
END $$;

-- pgvector >= 0.8. Senza iterative_scan la ricerca ANN filtrata per cluster
-- restituisce k vicini globali e poi ne scarta la maggior parte, con crollo
-- della recall quando il cluster è una fetta piccola del corpus. Con
-- iterative_scan l'indice continua a essere scandito finché non ci sono
-- abbastanza righe che passano il filtro.
-- relaxed_order e non strict_order: l'ordine esatto non ci interessa, subito
-- dopo passa il reranker.
DO $$
BEGIN
  EXECUTE format('ALTER DATABASE %I SET hnsw.iterative_scan TO %L', current_database(), 'relaxed_order');
  EXECUTE format('ALTER DATABASE %I SET hnsw.ef_search TO %L', current_database(), '100');
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'hnsw.iterative_scan non impostabile (pgvector < 0.8?): %', SQLERRM;
END $$;

CREATE OR REPLACE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

COMMENT ON FUNCTION set_updated_at() IS 'Trigger BEFORE UPDATE: mantiene updated_at.';
