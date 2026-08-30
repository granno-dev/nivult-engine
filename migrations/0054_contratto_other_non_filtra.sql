-- 0054 — «Other» esce dai tipi di contratto offerti nel pannello.
--
-- `ai_employment_type = 'OTHER'` non è un contratto insolito: è ciò che
-- la fonte non ha saputo classificare. Le offerte che lo portano, lette,
-- sono normali posizioni a tempo indeterminato — «Addetto/a risorse umane
-- a tempo indeterminato» è etichettata OTHER.
--
-- Misurato sul cluster Risorse umane × Italia: 116 su 281, il gruppo più
-- numeroso. Chi spuntava «solo tempo pieno» le perdeva tutte, per un
-- motivo che non aveva scelto — lo stesso errore del `false` sul visto
-- (0052) e della stringa vuota sul settore (0053).
--
-- Due parti, e servono entrambe: il funnel smette di escluderle
-- (matching/funnel.py, `OTHER` trattato come NULL) e il pannello smette
-- di offrirle come scelta. Offrire un filtro il cui valore significa
-- «non classificato» è offrire un filtro che nessuno può usare con
-- senso: chi lo spunta non sta chiedendo niente di preciso.
DELETE FROM filter_values
 WHERE parameter = 'ai_employment_type' AND api_value = 'OTHER';

-- Nessuna ricerca l'aveva selezionato (verificato prima), ma la pulizia
-- sta qui lo stesso: un valore rimasto in un array dopo che è uscito dal
-- vocabolario farebbe fallire il PUT successivo dell'utente con un 422
-- su un valore che lui non ha mai scelto di suo.
UPDATE user_clusters
   SET employment_types = array_remove(employment_types, 'OTHER')
 WHERE 'OTHER' = ANY(employment_types);
