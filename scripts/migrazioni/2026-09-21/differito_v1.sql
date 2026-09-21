-- 21/09/2026 — v1 differisce le offerte senza testo invece di classificarle
-- dal titolo. Un EXISTS sul jsonb nella query del demone costava 78 s a lotto
-- (2 min contro 50 s, misurato con EXPLAIN ANALYZE): il controllo si fa in
-- Python sulle righe prese, e chi non ha testo viene timbrato qui, escluso per
-- due ore e riprovato — fino a 7 giorni dalla creazione.
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS differito_v1_at timestamptz;
