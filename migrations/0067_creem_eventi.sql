-- 0067 — La deduplica dei webhook Creem (28/09/2026).
--
-- Nasceva a runtime dentro applica_pagamento, ma il ruolo dell'API e'
-- least-privilege (0010): niente CREATE TABLE fuori dalle migrazioni.
-- Una riga per event id: le riconsegne di Creem non accreditano due
-- volte la stessa ricarica.

CREATE TABLE creem_eventi (
  id          text        PRIMARY KEY,
  ricevuto_at timestamptz NOT NULL DEFAULT now()
);
