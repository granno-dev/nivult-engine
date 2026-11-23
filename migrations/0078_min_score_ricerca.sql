-- 0078: la soglia di score e' della ricerca, non del motore.
--
-- L'app espone uno slider 50-95 ma il motore valutava tutto contro la
-- costante 70: chi alzava la barra a 80 riceveva lo stesso i match da 75.
-- Ora la soglia si persiste per ricerca (user_clusters) e il worker la
-- scrive in matches.threshold_used — da cui `passed` e' generata, quindi
-- la selezione del digest si adegua da sola.
--
-- NULL = il default del motore (70): le ricerche esistenti e Nivult non
-- cambiano comportamento, e il rilascio non ha bisogno di backfill.
ALTER TABLE user_clusters ADD COLUMN min_score integer;

ALTER TABLE user_clusters ADD CONSTRAINT user_clusters_min_score_ck CHECK (
  min_score IS NULL OR (min_score >= 50 AND min_score <= 95)
);

COMMENT ON COLUMN user_clusters.min_score IS
  'Soglia minima di score per questa ricerca (slider dell''app); NULL = default del motore (70).';
