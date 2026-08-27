-- Da UN canale a UN INSIEME di canali.
--
-- Il modello a canale singolo era una scelta di comodo, non di prodotto:
-- chi vuole il digest su Telegram di solito lo vuole ANCHE via email — la
-- chat per leggerlo subito, la casella per ritrovarlo. Costringere a
-- sceglierne uno trasformava un'aggiunta in una rinuncia.
--
-- L'array sostituisce la colonna invece di affiancarla: due fonti di
-- verita' sullo stesso fatto sono il modo in cui una delle due mente.

ALTER TABLE users ADD COLUMN delivery_channels text[] NOT NULL DEFAULT '{email}';

UPDATE users SET delivery_channels = ARRAY[delivery_channel];

-- Le stesse garanzie di prima, in forma di insieme: solo canali noti,
-- mai vuoto, e ogni canale col suo recapito. L'email non ha ramo perche'
-- users.email e' NOT NULL.
ALTER TABLE users ADD CONSTRAINT users_channels_ck CHECK (
      delivery_channels <@ ARRAY['email','telegram','whatsapp']
  AND cardinality(delivery_channels) >= 1
  AND (NOT ('telegram' = ANY(delivery_channels)) OR telegram_chat_id IS NOT NULL)
  AND (NOT ('whatsapp' = ANY(delivery_channels)) OR whatsapp_e164   IS NOT NULL)
);

ALTER TABLE users DROP CONSTRAINT users_channel_address_ck;
ALTER TABLE users DROP COLUMN delivery_channel;

-- digests.channel resta a valore singolo: ogni CONSEGNA avviene su un
-- canale; e' l'utente ad averne piu' d'uno. Il worker registra il canale
-- principale (email se attiva, altrimenti il primo) e gli altri esiti
-- nell'error/log del digest.
