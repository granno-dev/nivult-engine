-- 0077: il canale 'app' — la consegna e' la disponibilita' nell'app nativa.
--
-- Per Mavult il digest non e' piu' una lettera spedita: e' una pagina che
-- esiste (la tab Digest legge digest_items) e la push nativa e' l'avviso.
-- 'app' entra nell'insieme dei canali ammessi; non ha recapito da provare
-- (a differenza di telegram/whatsapp) perche' il recapito e' l'account
-- stesso — la riga users che gia' esiste.
--
-- La forma e' additiva: chi non ha 'app' fra i canali passa lo stesso
-- controllo di prima, e Nivult non cambia comportamento. Vale per il DB
-- mavult e, quando arrivera', per quello nivult.
ALTER TABLE users DROP CONSTRAINT users_channels_ck;

ALTER TABLE users ADD CONSTRAINT users_channels_ck CHECK (
      delivery_channels <@ ARRAY['email','telegram','whatsapp','app']
  AND cardinality(delivery_channels) >= 1
  AND (NOT ('telegram' = ANY(delivery_channels)) OR telegram_chat_id IS NOT NULL)
  AND (NOT ('whatsapp' = ANY(delivery_channels)) OR whatsapp_e164   IS NOT NULL)
);

-- Stesso allargamento sullo snapshot del digest: il worker registra il
-- canale principale in digests.channel, e per un utente solo-app sarebbe
-- 'app'. Il vincolo della 0005 (nome automatico digests_channel_check)
-- lo rifiuterebbe a ogni INSERT.
ALTER TABLE digests DROP CONSTRAINT digests_channel_check;

ALTER TABLE digests ADD CONSTRAINT digests_channel_check CHECK (
  channel IN ('email','telegram','whatsapp','app')
);
