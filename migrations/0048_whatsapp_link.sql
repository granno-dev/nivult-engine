-- Collegamento del numero WhatsApp.
--
-- Stesso vincolo di fondo di Telegram, per una ragione diversa. Un'azienda
-- su WhatsApp PUO' scrivere per prima, ma solo con un template a pagamento —
-- e soprattutto: se bastasse digitare un numero nel pannello, chiunque
-- potrebbe inserire il numero di qualcun altro e fargli piovere addosso i
-- propri digest. La prova di possesso quindi la da' l'utente scrivendo per
-- primo: wa.me/<numero>?text=NIVULT <gettone>, il messaggio arriva nella
-- nostra inbox, e il gettone dentro il testo dice CHI sta collegando.
--
-- Effetto collaterale prezioso: quel primo messaggio apre la finestra di
-- servizio di 24 ore, dentro la quale le nostre risposte sono gratuite e
-- senza template. La conferma di collegamento non costa niente.
--
-- Il gettone segue la regola di login_tokens: in tabella SOLO lo sha256,
-- vita dieci minuti.

CREATE TABLE whatsapp_link_tokens (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   char(64)    NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  consumed_at  timestamptz,
  -- Il numero che ha consumato il gettone: la traccia di CHI ha collegato,
  -- come telegram_link_tokens.chat_id.
  phone_e164   text,

  CONSTRAINT whatsapp_link_hash_key  UNIQUE (token_hash),
  CONSTRAINT whatsapp_link_window_ck CHECK (expires_at > created_at),
  CONSTRAINT whatsapp_link_consumed_ck
    CHECK (consumed_at IS NULL OR consumed_at >= created_at),
  CONSTRAINT whatsapp_link_phone_ck
    CHECK ((consumed_at IS NULL) = (phone_e164 IS NULL))
);

CREATE INDEX whatsapp_link_user_idx ON whatsapp_link_tokens (user_id, created_at DESC);
CREATE INDEX whatsapp_link_expiry_idx ON whatsapp_link_tokens (expires_at)
  WHERE consumed_at IS NULL;

-- Un numero WhatsApp appartiene a UN utente solo: stessa ragione della chat
-- Telegram e di oauth_identities — l'indirizzo a cui si consegna identifica
-- una persona, e due account sullo stesso numero significherebbero digest
-- di uno recapitati all'altro.
CREATE UNIQUE INDEX users_whatsapp_e164_key ON users (whatsapp_e164)
  WHERE whatsapp_e164 IS NOT NULL;

-- La conversazione su Zernio. Serve per rileggere i messaggi IN ARRIVO:
-- il template promette «Reply STOP to stop receiving this digest», e una
-- promessa stampata in ogni digest va mantenuta — il worker controlla la
-- conversazione prima di ogni invio. Senza quest'id dovremmo cercare il
-- numero nell'inbox a ogni giro.
ALTER TABLE users ADD COLUMN whatsapp_conversation_id text;
