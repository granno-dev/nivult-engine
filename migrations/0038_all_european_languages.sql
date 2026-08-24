-- 0038 — Le lingue europee, tutte, e non solo quelle già viste.
--
-- Il vocabolario conteneva soltanto ciò che era già stato ingerito, e la
-- validazione dei filtri rifiuta ciò che non c'è. Il risultato era che un
-- utente non poteva scegliere lo spagnolo — e l'interfaccia gli offriva un
-- "richiedila" al posto di una casella.
--
-- Aveva senso finché i mercati aperti erano tre e fissi. Non ne ha più da
-- quando l'utente può aprire il mercato che vuole: apre la Spagna, le
-- offerte spagnole arrivano quella notte, e la lingua deve poterla scegliere
-- PRIMA, non dopo essersi accorto che il filtro non la contempla.
--
-- `evidence` distingue già i due casi e non serve inventare nulla:
-- 'verified' = vista nei dati, 'docs_only' = documentata e non ancora
-- osservata. L'API espone la differenza, così l'interfaccia può dire quali
-- lingue stanno già arrivando senza impedire di sceglierne un'altra.
INSERT INTO filter_values (parameter, api_value, response_value, label,
                           sort_order, evidence)
SELECT 'ai_language', l, l, l, 100 + row_number() OVER (ORDER BY l), 'docs_only'
FROM unnest(ARRAY[
  'Bulgarian','Croatian','Czech','Danish','Dutch','Estonian','Finnish',
  'Greek','Hungarian','Icelandic','Irish','Latvian','Lithuanian','Maltese',
  'Norwegian','Polish','Romanian','Slovak','Slovenian','Spanish'
]) AS l
WHERE NOT EXISTS (
  SELECT 1 FROM filter_values f
  WHERE f.parameter = 'ai_language' AND f.api_value = l
);
