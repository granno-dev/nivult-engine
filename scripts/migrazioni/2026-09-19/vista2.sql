-- Rifatta perche' una vista creata con SELECT * non vede le colonne aggiunte dopo:
-- `dominio_e_carriere` non compariva (19/09/2026). Qui i campi sono nominati, cosi'
-- si sa sempre cosa si vende e aggiungere una colonna e' una scelta, non un caso.
CREATE OR REPLACE VIEW aziende_pronte AS
SELECT company_id, nome, nome_fonte, piattaforma, slug,
       dominio, dominio_fonte, dominio_e_carriere,
       paese, settore, dipendenti, lei,
       offerte_attive, ultima_offerta, offerte_lette,
       tecnologie, n_tecnologie, nomi_dichiarati, canonico_id, aggiornato_at
  FROM aziende_vendibili
 WHERE NOT e_bacheca
   AND nome IS NOT NULL
   AND (canonico_id IS NULL OR canonico_id = company_id);
GRANT SELECT ON aziende_pronte TO nivult_operaio, nivult_app, nivult_lettore;
