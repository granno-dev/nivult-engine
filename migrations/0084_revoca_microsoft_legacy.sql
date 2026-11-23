-- 0084 — chiusura preventiva delle credenziali Microsoft legacy.
--
-- Prima della 0082 il tenant non era registrato e un'identita' enterprise non
-- provata poteva agganciarsi tramite email. Non basta rifiutare il login
-- successivo: una sessione o una chiave API gia' ottenuta resterebbe valida.
-- Non potendo distinguere retroattivamente i tenant consumer, le identita'
-- legacy vanno ricollegate da una sessione autenticata. Crediti, quote e dati
-- degli account non cambiano; le chiavi interne con user_id NULL non entrano.

UPDATE sessions AS s
   SET revoked_at = now()
 WHERE s.revoked_at IS NULL
   AND s.origin = 'microsoft'
   AND EXISTS (
       SELECT 1 FROM oauth_identities AS o
        WHERE o.user_id = s.user_id
          AND o.provider = 'microsoft'
          AND o.link_method = 'legacy'
   );

UPDATE login_tokens AS t
   SET consumed_at = now()
 WHERE t.consumed_at IS NULL
   AND t.origin = 'microsoft'
   AND EXISTS (
       SELECT 1 FROM oauth_identities AS o
        WHERE o.user_id = t.user_id
          AND o.provider = 'microsoft'
          AND o.link_method = 'legacy'
   );

UPDATE api_chiavi AS k
   SET revoked_at = now()
 WHERE k.revoked_at IS NULL
   AND EXISTS (
       SELECT 1 FROM oauth_identities AS o
        WHERE o.user_id = k.user_id
          AND o.provider = 'microsoft'
          AND o.link_method = 'legacy'
   );

UPDATE oauth_identities AS o
   SET revoked_at = COALESCE(o.revoked_at, now())
 WHERE o.provider = 'microsoft'
   AND o.link_method = 'legacy'
   AND o.revoked_at IS NULL;
