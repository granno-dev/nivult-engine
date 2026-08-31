-- I piani come venduti su Creem, decisi dal proprietario il 2026-09-01:
-- prova di 14 giorni con carta per tutti, e la scala delle ricerche
-- passa da 2/6/12 a 2/4/6 — «12 ricerche sono troppe, rischiamo che ci
-- sfuggano i costi». Le valutazioni scendono in proporzione: sono LORO
-- il rubinetto vero della spesa GLM, le ricerche sono la promessa
-- visibile. 8.000 al mese su Ultra = ~44 valutazioni al giorno per
-- ricerca, ancora largo.
UPDATE plan_quotas SET max_searches = 4, monthly_evaluations = 4000,
       note = 'circa 12 cluster stretti' WHERE plan = 'pro';
UPDATE plan_quotas SET max_searches = 6, monthly_evaluations = 8000,
       note = 'circa 25 cluster stretti' WHERE plan = 'ultra';
