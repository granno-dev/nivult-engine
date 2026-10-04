-- 0069 — Il registro dei consumi del portale B2B (04/10/2026).
--
-- Chi paga pretende di vedere DOVE vanno i crediti: una riga per evento
-- (chiamata API, rivelazione, export) con via, azione, dettaglio e quanti
-- crediti e' costato. La card «Credits» mostra il saldo; questa tabella
-- mostra la storia — e il dettaglio apre dal bottone in dashboard.

CREATE TABLE portale_uso (
  id         bigserial   PRIMARY KEY,
  user_id    uuid        NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  at         timestamptz NOT NULL DEFAULT now(),
  via        text        NOT NULL,   -- 'api' | 'portale' | 'export'
  azione     text        NOT NULL,   -- l'endpoint, o «rivelazione azienda»…
  dettaglio  text,                   -- la query, l'azienda, il file
  crediti    int         NOT NULL DEFAULT 1
);
CREATE INDEX portale_uso_utente_idx ON portale_uso (user_id, at DESC);
GRANT SELECT, INSERT ON portale_uso TO nivult_app;
GRANT USAGE, SELECT ON SEQUENCE portale_uso_id_seq TO nivult_app;
