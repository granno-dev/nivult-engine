-- La sintesi che esce e' quella ripulita dalle cifre inventate.
--
-- IL PERCHE'. Misurato il 20/09/2026 su 1.500 sintesi prese a caso: nel 13,2%
-- di quelle di mT5 c'e' una cifra di paga che in TUTTO il raw dell'annuncio non
-- esiste. Due forme, tutte e due cattive:
--   - un numero vero con le cifre scambiate: l'annuncio dice $85.389-$116.975,
--     la sintesi scrive $116.775;
--   - un range tondo inventato di sana pianta ($90.000-$110.000) perche' gli
--     annunci americani di solito ne hanno uno e il modello ha imparato a
--     metterlo.
-- Il 2B, sullo stesso metro e sullo stesso tipo di campione, sta allo 0,3%: non
-- e' un difetto dei generativi, e' di questo modello. E il 42,4% delle sintesi
-- che escono da `sintesi_finali` sono di mT5.
--
-- LA CURA, la stessa gia' usata per le tecnologie: si tiene solo cio' che nel
-- testo si puo' puntare col dito. L'unita' e' la frase — un numero sbagliato
-- avvelena la frase che lo contiene, non tutta la sintesi. Sulle 58.580 gia'
-- scritte: 85,3% intatte, 14,7% perdono una frase, ZERO buttate; la lunghezza
-- media passa da 98 a 95 parole.
--
-- L'originale NON si tocca: resta in `sintesi`, la ripulita sta in
-- `sintesi_pulita`. Se domani il filtro si rivelasse troppo severo si torna
-- indietro cambiando questa vista, senza rigenerare 58.000 sintesi.
--
-- ATTENZIONE alle righe non ancora passate dal filtro (`pulita_at IS NULL`):
-- qui valgono come «senza sintesi». E' la direzione giusta — meglio nessuna
-- sintesi che un salario inventato — ma vuol dire che il demone sul N5 deve
-- avere il codice che scrive `sintesi_pulita`, o le nuove sparirebbero.
ALTER TABLE sintesi_mt5 ADD COLUMN IF NOT EXISTS sintesi_pulita text;
ALTER TABLE sintesi_mt5 ADD COLUMN IF NOT EXISTS pulita_at timestamptz;
ALTER TABLE sintesi_mt5 ADD COLUMN IF NOT EXISTS frasi_tolte int;

CREATE OR REPLACE VIEW sintesi_finali AS
SELECT j.id AS job_id,
       coalesce(CASE WHEN m.ripassata_at IS NOT NULL THEN m.sintesi_pulita END,
                e.sintesi, m.sintesi_pulita)            AS sintesi,
       CASE WHEN m.ripassata_at IS NOT NULL AND m.sintesi_pulita IS NOT NULL THEN 'nivult-2b+ripasso'
            WHEN e.sintesi IS NOT NULL                                       THEN 'nivult-2b'
            WHEN m.sintesi_pulita IS NOT NULL                                THEN 'nivult-mt5'
       END                                              AS da,
       m.fiducia                                        AS fiducia_mt5,
       m.ripassata_at                                   AS ripassata_at,
       m.sintesi_originale                              AS sintesi_mt5_originale,
       m.frasi_tolte                                    AS frasi_tolte
  FROM ats_jobs j
  LEFT JOIN estrazioni_v2b e ON e.job_id = j.id
  LEFT JOIN sintesi_mt5   m ON m.job_id = j.id
 WHERE coalesce(e.sintesi, m.sintesi_pulita) IS NOT NULL;

GRANT SELECT ON sintesi_finali TO nivult_operaio;
