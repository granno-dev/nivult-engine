-- 0055 — La soglia del digest scende da 80 a 70.
--
-- A 80 passava solo la metà ALTA della fascia che la rubrica chiama
-- «buona: il ruolo è giusto, con qualche scarto di livello o di ambito».
-- Misurato su un utente reale, sulle offerte ancora vive: 10 sopra 80,
-- 18 sopra 70. Le sette escluse non erano scarti — HR Business Partner
-- in Amazon, Global HR Business Partner, People & HR Operations
-- Specialist Italia, Change Specialist Milano.
--
-- `matches.passed` è generata da `score >= threshold_used`, quindi
-- abbassare `threshold_used` la fa risalire da sola.
--
-- SOLO ciò che non è mai stato consegnato e il cui annuncio è ancora
-- vivo. Sulle righe già finite in un digest il vecchio valore RESTA: è
-- il registro di quale asticella hanno superato quel giorno, e riscriverlo
-- sarebbe raccontare una storia diversa da quella successa. Il nuovo
-- metro vale da qui in avanti, e su ciò che aspetta ancora il suo turno.
UPDATE matches m
   SET threshold_used = 70
  FROM jobs j
 WHERE j.id = m.job_id
   AND m.threshold_used > 70
   AND j.status = 'active'
   AND NOT EXISTS (SELECT 1 FROM digest_items di WHERE di.match_id = m.id);
