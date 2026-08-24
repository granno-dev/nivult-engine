-- 0041 — Il ruolo si scrive, il campo si ricava. E il filtro per settore.
--
-- ── 1. La classificazione ruolo → famiglia ─────────────────────────────────
--
-- L'utente scrive "HR Business Partner"; quale scaffale leggere (la famiglia
-- della tassonomia) è un fatto NOSTRO, e chiederglielo era fargli fare il
-- lavoro del motore. Lo ricava GLM, scegliendo fra le 33 famiglie.
--
-- La cache è condivisa fra tutti gli utenti e non scade: "hr business
-- partner" si classifica una volta sola al mondo, non una volta a utente.
-- Non c'è niente di personale dentro — è la coppia (titolo, famiglia), che
-- è conoscenza del mestiere, non di chi l'ha digitata.
CREATE TABLE role_family_cache (
  role_norm  text        PRIMARY KEY,
  family     text        NOT NULL REFERENCES job_families(family),
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE role_family_cache IS
  'Titolo digitato (normalizzato) -> famiglia della tassonomia. Una chiamata '
  'GLM alla prima occorrenza nel mondo, zero dalle successive.';

-- ── 2. Il settore ──────────────────────────────────────────────────────────
--
-- jobs.org_industry c'è già (73% del corpus attivo, solo Fantastic — le
-- fonti nazionali non ce l'hanno). Il filtro è sicuro per la stessa regola
-- di sempre: un campo NULL non esclude mai, quindi le offerte delle fonti
-- nazionali passano comunque e nessuno perde la Francia per aver chiesto
-- "Healthcare".
ALTER TABLE user_clusters
  ADD COLUMN industries text[] NOT NULL DEFAULT '{}';

COMMENT ON COLUMN user_clusters.industries IS
  'Settori accettati (vocabolario LinkedIn, dal corpus). Vuoto = tutti. '
  'NULL sull''offerta non esclude mai: vale solo dove il dato c''è.';
