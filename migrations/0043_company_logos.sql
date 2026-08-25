-- 0043 — I loghi aziendali, scaricati una volta e serviti da noi.
--
-- CLAUDE.md lo descrive come deciso da tempo — "il logo si scarica e si
-- salva UNA VOLTA PER AZIENDA, mai collegato al volo" — ma non era mai stato
-- costruito. Le due ragioni valgono entrambe, e sono diverse:
--
--   • Nelle email i client bloccano le immagini remote per impostazione
--     predefinita: un logo collegato a un dominio esterno resta un rettangolo
--     vuoto per la maggior parte dei destinatari.
--   • Sul sito funzionerebbe, ma collegare il CDN di LinkedIn significa
--     dire a LinkedIn ogni volta che qualcuno apre il proprio pannello. Su
--     un prodotto la cui promessa è che il CV resta cifrato e in UE, è uno
--     scambio sbagliato per una decorazione.
--
-- La chiave è per AZIENDA, non per offerta: la stessa azienda pubblica
-- decine di annunci e scaricare lo stesso file ogni volta sarebbe sprecato.
-- org_linkedin_slug (96%) con domain_derived (98%) come ripiego, esattamente
-- la catena già scritta in CLAUDE.md.
CREATE TABLE company_logos (
  chiave      text        PRIMARY KEY,
  mime        text,
  bytes       bytea,
  origine     text,
  fetched_at  timestamptz NOT NULL DEFAULT now(),

  -- Un tetto: oltre questa taglia non è un logo, è un errore di qualcuno.
  CONSTRAINT company_logos_size_ck
    CHECK (bytes IS NULL OR length(bytes) <= 512000)
);

-- `bytes` NULL con la riga presente significa "provato e non riuscito": si
-- ricorda anche il fallimento, o ogni apertura del pannello riproverebbe a
-- scaricare un file che non c'è, all'infinito.
COMMENT ON COLUMN company_logos.bytes IS
  'I byte del logo. NULL = tentato e fallito, e la riga esiste apposta: '
  'senza, ogni visita riproverebbe un download che non riesce.';

CREATE INDEX company_logos_stale_idx ON company_logos (fetched_at);
