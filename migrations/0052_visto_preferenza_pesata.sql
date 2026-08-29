-- 0052 — Il visto smette di essere un'esclusione e diventa una preferenza.
--
-- Il catalogo diceva «campo pieno al 100%», ed era vero ma fuorviante: il
-- campo è pieno di false che significano «l'annuncio non ne parla».
-- Misurato sulle offerte attive: 2.332 false, 2 true, 17 null. Un filtro
-- duro su quel campo non distingueva «chi sponsorizza» da «chi no» —
-- distingueva «chi lo scrive nell'annuncio» da tutti gli altri, e chi
-- spuntava la casella riceveva digest vuoti per sempre. È successo davvero,
-- al primo digest di un utente reale.
--
-- La spunta resta nel pannello e resta promessa: cambia il meccanismo.
-- Il bisogno viaggia nel prompt di valutazione come preferenza forte
-- (matching/funnel.py), e l'offerta che dichiara la sponsorizzazione lo
-- dice al modello (matching/llm.py). Nessuna esclusione deterministica.
UPDATE user_filters
   SET rationale = 'Preferenza pesata dal modello, non esclusione: il campo '
                   'è pieno al 100% ma il false significa quasi sempre '
                   '«l''annuncio non ne parla» (misurato: 2.332 false, 2 '
                   'true). Un''esclusione dura svuotava il digest. Il '
                   'bisogno entra nel prompt come preferenza forte e la '
                   'sponsorizzazione dichiarata viene premiata.'
 WHERE filter_key = 'visa_sponsorship';
