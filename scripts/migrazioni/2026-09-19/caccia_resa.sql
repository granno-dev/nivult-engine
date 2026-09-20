-- Quanto rende il cacciatore su ogni piattaforma (19/09/2026).
--
-- Finora pescava «prima chi ha piu' offerte», che e' ragionevole ma ignora la
-- probabilita' di riuscita. E le differenze sono enormi, misurate:
--   Vincere 67%, Zoho Recruit 52%, Catsone 38%, Cornerstone 33%, Workday 24%
--   ... Lever 7%, Greenhouse 4%, Ashby 3%
-- A parita' di tempo, provare prima Zoho che Ashby vale diciassette volte tanto.
--
-- La resa NON si scrive a mano: si ricalcola dai dati a ogni giro del demone,
-- cosi' non invecchia e si aggiusta da sola quando una fonte migliora — come
-- SearXNG, riparato oggi.
CREATE TABLE IF NOT EXISTS caccia_resa (
  platform_id   text PRIMARY KEY,   -- text in tutto lo schema ATS, non uuid
  provate       int  NOT NULL,
  trovate       int  NOT NULL,
  resa          real,          -- NULL finche' le prove sono troppo poche per dire
  aggiornato_at timestamptz NOT NULL DEFAULT now());

ALTER TABLE caccia_resa OWNER TO nivult;
GRANT SELECT, INSERT, UPDATE, DELETE ON caccia_resa TO nivult_operaio, nivult_app;
