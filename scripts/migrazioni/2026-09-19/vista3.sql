-- Il livello del dominio deve arrivare a chi compra: e' la differenza fra «provato»
-- e «corrisponde al nome». Una vista con SELECT * non vedrebbe la colonna nuova.
DROP VIEW IF EXISTS aziende_pronte;
CREATE VIEW aziende_pronte AS
SELECT company_id, nome, nome_fonte, piattaforma, slug,
       dominio, dominio_fonte, dominio_livello, dominio_e_carriere,
       paese, settore, dipendenti, lei,
       offerte_attive, ultima_offerta, offerte_lette,
       tecnologie, n_tecnologie, nomi_dichiarati, canonico_id, aggiornato_at
  FROM aziende_vendibili
 WHERE NOT e_bacheca AND nome IS NOT NULL
   AND (canonico_id IS NULL OR canonico_id = company_id);
GRANT SELECT ON aziende_pronte TO nivult_operaio, nivult_app, nivult_lettore;
