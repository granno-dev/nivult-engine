-- 0062 — Le chiavi dell'API clienti B2B (il livello /v1).
--
-- Nivult vende il dataset: offerte e aziende escono da /v1 dietro una
-- chiave per cliente, nell'header X-Api-Key — MAI in query string, perche'
-- le URL finiscono nei log dei proxy e dei browser, e la stessa regola vale
-- gia' per i token di sessione.
--
-- In tabella ci va SOLO lo sha256 esadecimale della chiave, come per
-- login_tokens e telegram_link_tokens: se il database trapela, quello che
-- si trova non apre niente. La chiave in chiaro esiste una volta sola,
-- stampata dal comando `python -m nivult.api_clienti.chiavi nuova` nel
-- terminale di chi la crea.
--
-- I crediti sono la misura della vendita: una chiamata = un credito (non
-- una riga — il prezzo non cambia se la pagina e' piena). Il contatore e'
-- MENSILE e si azzera da solo al cambio di mese: mese_uso < primo del mese
-- corrente vuol dire che il conteggio e' di un mese vecchio. Niente cron,
-- niente job notturno: il reset e' una lettura, non una scrittura
-- programmata che puo' non girare.

CREATE TABLE api_chiavi (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  -- sha256 esadecimale della chiave: 64 caratteri [0-9a-f], come altrove.
  key_hash        text        UNIQUE NOT NULL
                              CHECK (key_hash ~ '^[0-9a-f]{64}$'),
  -- A chi appartiene, in parole nostre («cliente X»): la vediamo noi in
  -- `lista`, non e' un dato del cliente.
  label           text        NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  -- Revoca logica, mai DELETE: la riga resta come traccia di chi aveva
  -- accesso a cosa, e `lista` continua a mostrarla come revocata.
  revoked_at      timestamptz,
  crediti_mensili integer     NOT NULL DEFAULT 10000
                              CHECK (crediti_mensili >= 0),
  usati_mese      integer     NOT NULL DEFAULT 0
                              CHECK (usati_mese >= 0),
  -- Il mese a cui si riferisce usati_mese. Alla creazione e' il giorno
  -- corrente: il confronto e' sempre «minore del primo del mese corrente»,
  -- quindi una data di meta' mese non innesca mai un reset falso.
  mese_uso        date        NOT NULL DEFAULT CURRENT_DATE,

  CONSTRAINT api_chiavi_revoca_ck
    CHECK (revoked_at IS NULL OR revoked_at >= created_at)
);

-- Le chiavi sono poche (una per cliente) e si cercano solo per hash:
-- l'indice della UNIQUE basta, niente altro da mantenere.
