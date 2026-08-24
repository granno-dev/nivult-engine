-- 0032 — Lo stato del giro OAuth, fra /start e /callback.
--
-- oauth_identities esiste già dalla 0009, con chiave (provider, subject) e non
-- l'email. Qui manca solo il pezzo effimero: ciò che dobbiamo ricordare fra il
-- momento in cui mandiamo l'utente da Google e il momento in cui torna.

-- ── 1. Il flusso in corso ───────────────────────────────────────────────────
--
-- Non ha user_id, e non è una dimenticanza: a /start non sappiamo ancora CHI
-- sta entrando. Lo scopriremo solo dal token del provider, al ritorno.
--
-- Da qui una conseguenza sulla privacy che vale la pena scrivere: siccome la
-- riga non è agganciabile a un utente, non può nemmeno seguirlo nella
-- cancellazione. Quindi qui NON entrano dati personali — niente ip, niente
-- user agent, al contrario di login_tokens che invece un utente ce l'ha.
-- Ciò che non si può cancellare su richiesta, non si raccoglie.
CREATE TABLE oauth_flows (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  provider      text        NOT NULL CHECK (provider IN ('google','microsoft')),

  -- Dello state teniamo solo lo sha256: al ritorno va CONFRONTATO, e per
  -- confrontare l'impronta basta. Stessa disciplina di login_tokens e sessions.
  state_hash    char(64)    NOT NULL CHECK (state_hash ~ '^[0-9a-f]{64}$'),
  -- Idem per il nonce, che confronteremo col claim dentro l'id_token.
  nonce_hash    char(64)    NOT NULL CHECK (nonce_hash ~ '^[0-9a-f]{64}$'),

  -- Il verifier PKCE invece sta in chiaro, ed è deliberato: non va
  -- confrontato, va RISPEDITO al provider allo scambio del code. Hashato
  -- sarebbe inservibile. È a vita corta, monouso, e da solo non apre niente:
  -- senza l'authorization code abbinato non vale nulla.
  code_verifier text        NOT NULL CHECK (length(code_verifier) BETWEEN 43 AND 128),

  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz NOT NULL,
  consumed_at   timestamptz,

  CONSTRAINT oauth_flows_state_key   UNIQUE (state_hash),
  CONSTRAINT oauth_flows_window_ck   CHECK (expires_at > created_at),
  CONSTRAINT oauth_flows_consumed_ck CHECK (consumed_at IS NULL OR consumed_at >= created_at)
);

-- Serve alla potatura delle righe scadute, che gira a ogni /start: sono righe
-- che vivono dieci minuti, non devono accumularsi per sempre.
CREATE INDEX oauth_flows_expiry_idx ON oauth_flows (expires_at);

COMMENT ON TABLE oauth_flows IS
  'Stato effimero del giro OAuth. Nessun dato personale: la riga nasce prima '
  'che si sappia chi sta entrando, quindi non seguirebbe l''utente nella '
  'cancellazione.';


-- ── 2. Da dove è nato un token di accesso ───────────────────────────────────
--
-- Il callback OAuth non emette una sessione: emette un token monouso di
-- login_tokens e rimanda il browser su /verify, la pagina che già esiste e sa
-- fare quel mestiere. Il motivo è che così il token di SESSIONE non viaggia
-- mai dentro una URL — niente referrer, niente cronologia del browser, niente
-- log dei proxy. In URL viaggia solo un gettone che vale una volta sola e
-- cinque minuti.
--
-- Ma allora la sessione che ne nasce non deve risultare 'magic_link', o
-- sessions.origin mentirebbe proprio nel momento in cui serve: quando si
-- indaga su un accesso sospetto. L'origine è una proprietà di come il token è
-- stato emesso, quindi sta sul token e non la passa il chiamante.
ALTER TABLE login_tokens
  ADD COLUMN origin text NOT NULL DEFAULT 'magic_link'
    CHECK (origin IN ('magic_link','google','microsoft'));

COMMENT ON COLUMN login_tokens.origin IS
  'Come è nato il token: il magic link via email, oppure un ritorno OAuth. '
  'Viene copiato in sessions.origin al consumo.';
