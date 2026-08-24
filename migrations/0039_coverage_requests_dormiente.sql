-- 0039 — coverage_requests resta, ma dice cosa è diventata.
--
-- Nasceva un'ora fa per raccogliere i mercati che non coprivamo, quando
-- l'onboarding mostrava tre cluster fissi. Poi l'utente ha potuto APRIRE il
-- mercato che voleva, e il meccanismo di richiesta è morto lo stesso giorno:
-- la domanda ora si legge nei cluster che vengono creati, con dentro chi si
-- iscrive.
--
-- La tabella non si elimina, e non è pigrizia: contiene una richiesta vera —
-- "HR Business Partner · IT" — e quella riga ha insegnato qualcosa. Non è un
-- mercato mancante: è un TITOLO di ruolo, dentro la famiglia Human Resources.
-- Diceva che il selettore non chiariva la granularità, ed è stato corretto
-- nel sito con una riga di aiuto.
--
-- Buttare un dato reale per togliere una tabella inerte è uno scambio
-- perdente. Resta qui, senza rotta che ci scriva, finché non serve o finché
-- non si decide di archiviarla con cognizione.
COMMENT ON TABLE coverage_requests IS
  'DORMIENTE dal 2026-08-25: nessuna rotta ci scrive piu''. La domanda si '
  'legge nei cluster aperti dagli utenti. Conservata per il dato storico '
  'che contiene, non da estendere senza riaprire la decisione.';
