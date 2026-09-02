-- Schema del sistema ATS autonomo. Database SEPARATO dal motore principale.
-- Vive in nivult_ats, non tocca nivult in nessun modo.

CREATE TABLE IF NOT EXISTS ats_platforms (
    id          TEXT PRIMARY KEY,      -- 'greenhouse', 'smartrecruiters', …
    name        TEXT NOT NULL,
    api_type    TEXT NOT NULL,         -- 'json' (API pubblica) o 'html' (scraping)
    is_active   BOOLEAN NOT NULL DEFAULT true,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS ats_companies (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_id     TEXT NOT NULL REFERENCES ats_platforms(id),
    slug            TEXT NOT NULL,     -- il nome usato nell'URL dell'API
    company_name    TEXT,              -- nome leggibile, se noto
    country         TEXT,              -- paese principale, se noto
    is_active       BOOLEAN NOT NULL DEFAULT true,
    last_fetch_at   TIMESTAMPTZ,
    job_count       INTEGER NOT NULL DEFAULT 0,
    -- Come abbiamo scoperto questa azienda: 'existing_db' (dal motore),
    -- 'manual', 'discovery'
    discovered_from TEXT NOT NULL DEFAULT 'manual',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Workday: server (wd3, wd103...) e istanza del tenant.
    wd_server       TEXT,
    wd_instance     TEXT,
    -- In-recruiting: la chiave di pubblicazione che annunci.php esige,
    -- scavata dagli embed archiviati (Wayback) e convalidata dal vivo.
    pub_key         TEXT,
    UNIQUE(platform_id, slug)
);

CREATE TABLE IF NOT EXISTS ats_jobs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform_id TEXT NOT NULL REFERENCES ats_platforms(id),
    slug        TEXT NOT NULL,         -- chi l'ha pubblicata (per join con ats_companies)
    external_id TEXT NOT NULL,         -- l'id dell'offerta nell'ATS
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,         -- link diretto all'offerta sul sito dell'azienda
    location    TEXT,                  -- località grezza come viene dall'API
    country     TEXT,                  -- ISO se determinabile
    city        TEXT,
    posted_at   TIMESTAMPTZ,           -- quando l'azienda l'ha pubblicata
    department  TEXT,                  -- dipartimento/team se disponibile
    raw         JSONB,                 -- la risposta API integrale, per sempre
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(platform_id, external_id)
);

CREATE INDEX IF NOT EXISTS ats_jobs_platform_idx ON ats_jobs(platform_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS ats_jobs_title_idx ON ats_jobs USING gin(to_tsvector('simple', title));

-- Cache dell'arricchimento Wikidata: una riga per azienda, per sempre.
CREATE TABLE IF NOT EXISTS organizations (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL UNIQUE,  -- il nome dal campo organization dell'offerta
    wikidata_id   TEXT,
    employees     INTEGER,
    industry      TEXT,
    logo_url      TEXT,               -- URL del logo su Wikimedia Commons
    website       TEXT,
    country       TEXT,
    enriched_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- La classificazione in famiglia professionale, fatta da GLM.
CREATE TABLE IF NOT EXISTS job_classifications (
    job_id        UUID PRIMARY KEY REFERENCES ats_jobs(id) ON DELETE CASCADE,
    family        TEXT,               -- la famiglia (Human Resources, Software, …)
    confidence    REAL,               -- 0-1, quanto GLM è sicuro
    model         TEXT NOT NULL,
    classified_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS job_classifications_family_idx ON job_classifications(family);

-- Il censimento dei domini aziendali per il detector.
-- Fonti: Wikidata (aziende con sito ufficiale), DB di produzione
-- (domain_derived). Il detector visita la homepage, segue il link
-- careers e identifica l'ATS dalle impronte nell'HTML.
CREATE TABLE IF NOT EXISTS company_domains (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain        TEXT NOT NULL UNIQUE,   -- il dominio del sito aziendale
    company_name  TEXT,
    country       TEXT,                   -- ISO del paese (Wikidata P17)
    employees     INTEGER,               -- per prioritarizzare le grandi
    source        TEXT NOT NULL,          -- 'wikidata' | 'production'
    -- stato del detector: 'pending' | 'ats' (piattaforma identificata) |
    -- 'no_ats' (pagina carriere senza impronte note) | 'no_careers' |
    -- 'error' | 'dead' (dominio che non risponde)
    status        TEXT NOT NULL DEFAULT 'pending',
    platform_id   TEXT,                   -- l'ATS identificato, se 'ats'
    careers_url   TEXT,                   -- la pagina carriere trovata
    careers_kind  TEXT,                   -- 'custom' | 'platform'
    checked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS company_domains_status_idx ON company_domains(status, employees DESC NULLS LAST);
