-- 0026 — Quali filtri promettiamo, e dove si applicano.
--
-- REGOLA DEFINITIVA SUL PUSHDOWN: in chiamata vanno solo PAESE e FAMIGLIA.
-- Tutti i filtri personali restano nel funnel.
--
-- Il risparmio di crediti non vale un archivio che dipende da chi era iscritto
-- quel giorno: un filtro spinto in chiamata restringe il corpus condiviso, e le
-- offerte non scaricate ieri non tornano quando domani si iscrive qualcuno con
-- preferenze più larghe. Nel funnel un filtro costa zero e non lascia buchi.
--
-- Sparisce quindi anche la firma dei filtri spinti: non c'è più niente da
-- firmare.
ALTER TABLE clusters DROP COLUMN pushdown_signature;


-- Dimensione azienda: soglie numeriche, non fasce.
--
-- organization_size come FILTRO vuole "2-10" mentre il CAMPO restituisce
-- "2-10 employees", e quell'ambiguità ci ha quasi fatto concludere che il dato
-- non fosse utilizzabile. Le soglie numeriche non hanno formati da sbagliare.
ALTER TABLE user_clusters DROP COLUMN company_sizes;
ALTER TABLE user_clusters
  ADD COLUMN min_headcount integer CHECK (min_headcount >= 0),
  ADD COLUMN max_headcount integer CHECK (max_headcount > 0),
  ADD CONSTRAINT user_clusters_headcount_ck
    CHECK (min_headcount IS NULL OR max_headcount IS NULL
           OR min_headcount <= max_headcount);

COMMENT ON COLUMN user_clusters.min_headcount IS
  'Soglia inferiore di dipendenti. LE OFFERTE SENZA IL DATO NON VANNO ESCLUSE: '
  'org_headcount arriva solo da Fantastic, e filtrarle via significherebbe che '
  'chi cerca grandi aziende perde tutte le offerte francesi e svedesi soltanto '
  'perché passate da un''altra fonte.';

DELETE FROM filter_bindings WHERE column_name = 'company_sizes';


-- Cosa promettiamo sul sito, e cosa no. In tabella perché l'onboarding possa
-- leggerla e non prometta all'utente un filtro che non sappiamo applicare.
CREATE TABLE user_filters (
  filter_key   text    PRIMARY KEY,
  label        text    NOT NULL,
  promised     boolean NOT NULL,
  fill_rate_pct smallint CHECK (fill_rate_pct BETWEEN 0 AND 100),
  rationale    text    NOT NULL
);

INSERT INTO user_filters (filter_key, label, promised, fill_rate_pct, rationale) VALUES
  ('work_arrangement','Sede, ibrido o remoto',        true, 100,
   'Campo pieno al 100% sul campione'),
  ('experience_level','Anni di esperienza',           true, 100,
   'Campo pieno al 100%, scala confermata sui dati reali'),
  ('employment_type','Tipo di contratto',             true, 100,
   'Campo pieno al 100%'),
  ('language','Lingua dell''annuncio',                true, 100,
   'Campo pieno al 100%. Attenzione: nome esteso, non codice ISO'),
  ('visa_sponsorship','Sponsorship del visto',        true, 100,
   'Campo pieno al 100%, anche se raro come valore positivo'),
  ('employer_kind','Agenzie o datore diretto',        true, 100,
   'Dichiarato dalla fonte al 100% su Fantastic, integrato dalla nostra lista'),
  ('company_headcount','Dimensione azienda',          true,  99,
   'Copertura 98,8% in Italia. Soglie numeriche, non fasce: il formato delle '
   'fasce differisce fra filtro e campo. Le offerte senza il dato NON si escludono'),
  ('education','Titolo di studio',                    false, 48,
   'Campo valorizzato solo sul 48%: un filtro su questo escluderebbe metà del '
   'mercato per assenza di dato, non per scelta dell''utente'),
  ('salary','Stipendio',                              false, 24,
   'Solo il 24% delle offerte lo dichiara. Si MOSTRA quando c''è, non si filtra: '
   'filtrarci nasconderebbe tre offerte su quattro');

COMMENT ON TABLE user_filters IS
  'I filtri che il sito promette all''utente. promised = false significa che il '
  'dato esiste ma la copertura non regge un filtro.';
