-- 0034 — Quello che l'utente vuole, detto con parole sue.
--
-- I filtri deterministici dicono cosa ESCLUDERE, e lo fanno bene: lingua,
-- contratto, seniority, modalità. Ma non c'è modo di dire "cerco un ruolo con
-- gestione di un team, non solo esecutivo", oppure "preferisco aziende
-- prodotto e non consulenza". Sono le cose che decidono se un'offerta vale il
-- tempo di chi legge, e finora non arrivavano al modello in nessuna forma.
--
-- Sta sul CLUSTER e non sull'utente perché è una preferenza DELLA RICERCA:
-- chi segue HR in Italia e Operations in Svezia cerca due cose diverse, e una
-- nota sola per entrambe sarebbe rumore su almeno una.
ALTER TABLE user_clusters ADD COLUMN wants text;

-- Un tetto c'è, e non è per lo spazio: questo testo entra nel prompt di OGNI
-- offerta valutata, quindi la sua lunghezza si paga moltiplicata per il
-- numero di offerte del cluster. Mille caratteri sono un paragrafo pieno —
-- abbastanza per dire cosa si cerca, troppo poco per incollarci un CV.
ALTER TABLE user_clusters
  ADD CONSTRAINT user_clusters_wants_ck
  CHECK (wants IS NULL OR length(wants) <= 1000);

COMMENT ON COLUMN user_clusters.wants IS
  'Cosa cerca l''utente, con parole sue, per QUESTA ricerca. Va al modello '
  'in coda all''offerta, non nel prefisso: il prefisso (CV + rubrica) deve '
  'restare identico fra le chiamate o la cache dei prompt smette di pagare.';
