-- 0035 — Anche le modalità di lavoro parlano all'utente.
--
-- La 0033 riempiva solo le etichette mancanti (`label IS NULL OR = ''`), e
-- queste c'erano già — in italiano. Il risultato è che il sito inglese
-- mostrava «In sede» e «Solo da remoto» accanto a «Full time».
--
-- Le etichette dicono cosa cambia per chi legge, non come è scritto il campo:
-- «Remote OK» sulla fonte significa che il remoto è ammesso, non che il ruolo
-- sia da remoto — ed è una distinzione che decide se un'offerta è pertinente.
UPDATE filter_values SET label = 'On-site'          WHERE parameter='ai_work_arrangement' AND api_value='On-site';
UPDATE filter_values SET label = 'Hybrid'           WHERE parameter='ai_work_arrangement' AND api_value='Hybrid';
UPDATE filter_values SET label = 'Remote possible'  WHERE parameter='ai_work_arrangement' AND api_value='Remote OK';
UPDATE filter_values SET label = 'Fully remote'     WHERE parameter='ai_work_arrangement' AND api_value='Remote Solely';
