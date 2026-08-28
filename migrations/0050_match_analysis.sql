-- 0050 — L'analisi di allineamento sul match.
--
-- Il pannello apre l'offerta in una finestra col dettaglio: pro e
-- attenzioni scritti da GLM contro il profilo del candidato. Si genera
-- alla PRIMA apertura e si salva qui: una chiamata per offerta aperta,
-- mai rigenerata. jsonb {"pros":[...],"cons":[...],"lang":"Italian"} —
-- nessuna query filtra su questi campi.
ALTER TABLE matches ADD COLUMN analysis jsonb;
