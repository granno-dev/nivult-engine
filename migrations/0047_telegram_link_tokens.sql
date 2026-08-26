-- Collegamento dell'account Telegram.
--
-- Un bot Telegram NON puo' scrivere per primo: finche' non e' l'utente ad
-- aprire la conversazione non esiste nessun chat_id a cui mandare. Il giro e'
-- quindi obbligato: gettone monouso -> deep link t.me/<bot>?start=<gettone>
-- -> l'utente preme START -> Telegram consegna "/start <gettone>" al webhook
-- -> li' si risolve il gettone e si salva il chat_id.
--
-- Il gettone e' un segreto e segue la regola gia' scritta per login_tokens:
-- in tabella ci va SOLO lo sha256. Se il database trapela, quello che si
-- trova non collega nessuna chat. E ha vita corta, dieci minuti, perche'
-- inoltrare quel link a qualcun altro significa regalargli i propri digest,
-- esattamente come col magic link.
--
-- Telegram accetta al massimo 64 caratteri nel parametro `start`, in
-- alfabeto base64url: 32 byte casuali ne fanno 43, e ci stanno.

CREATE TABLE telegram_link_tokens (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   char(64)    NOT NULL CHECK (token_hash ~ '^[0-9a-f]{64}$'),
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  consumed_at  timestamptz,
  -- Il chat_id che ha consumato il gettone. Resta qui come traccia di CHI ha
  -- collegato: se un domani un utente dice "ricevo i digest di un altro",
  -- users.telegram_chat_id dice solo lo stato finale, non da dove viene.
  chat_id      text,

  CONSTRAINT telegram_link_hash_key  UNIQUE (token_hash),
  CONSTRAINT telegram_link_window_ck CHECK (expires_at > created_at),
  CONSTRAINT telegram_link_consumed_ck
    CHECK (consumed_at IS NULL OR consumed_at >= created_at),
  -- Consumato senza chat_id non vuol dire niente: e' il chat_id il motivo
  -- per cui il gettone esiste.
  CONSTRAINT telegram_link_chat_ck
    CHECK ((consumed_at IS NULL) = (chat_id IS NULL))
);

CREATE INDEX telegram_link_user_idx ON telegram_link_tokens (user_id, created_at DESC);
CREATE INDEX telegram_link_expiry_idx ON telegram_link_tokens (expires_at)
  WHERE consumed_at IS NULL;

-- Una chat Telegram appartiene a UN utente solo.
--
-- Senza questo, due account potrebbero collegare lo stesso telefono e la
-- seconda persona riceverebbe i digest della prima senza che nessuno se ne
-- accorga. E' lo stesso ragionamento per cui oauth_identities ha la chiave su
-- (provider, subject): l'indirizzo a cui si consegna identifica una persona.
CREATE UNIQUE INDEX users_telegram_chat_key ON users (telegram_chat_id)
  WHERE telegram_chat_id IS NOT NULL;

-- Quante volte di fila la consegna su questo canale e' fallita.
--
-- Conta i guasti PASSEGGERI: rete, un 500 di Telegram. Al secondo di fila si
-- molla il canale e si torna all'email, invece di accumulare digest falliti
-- che nessuno guarda.
--
-- Il blocco del bot NON passa di qui: Telegram risponde 403 e continuera' a
-- farlo per sempre, quindi non e' un guasto da contare ma un canale che non
-- esiste piu' — si ripiega subito, alla prima volta. Un canale morto in
-- silenzio e' peggio di un canale mai acceso.
ALTER TABLE users
  ADD COLUMN delivery_failures int NOT NULL DEFAULT 0
    CHECK (delivery_failures >= 0);
