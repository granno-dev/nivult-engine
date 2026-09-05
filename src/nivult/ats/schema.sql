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
-- Guardia country a livello database: l'imbuto che ogni scrittore
-- (runner, feed_globale, servizi_pubblici, agenzie, headless, e
-- qualunque fonte futura) attraversa. Nato dopo aver trovato 71 codici
-- non-ISO ('中国', 'M1', '26'...) sfuggiti da vie diverse: enforce in
-- Postgres, non promesso dal codice.
CREATE TABLE IF NOT EXISTS iso_paesi (code text PRIMARY KEY);
INSERT INTO iso_paesi (code) VALUES
 ('AD'),('AE'),('AF'),('AG'),('AI'),('AL'),('AM'),('AO'),('AQ'),('AR'),
 ('AS'),('AT'),('AU'),('AW'),('AX'),('AZ'),('BA'),('BB'),('BD'),('BE'),
 ('BF'),('BG'),('BH'),('BI'),('BJ'),('BL'),('BM'),('BN'),('BO'),('BQ'),
 ('BR'),('BS'),('BT'),('BV'),('BW'),('BY'),('BZ'),('CA'),('CC'),('CD'),
 ('CF'),('CG'),('CH'),('CI'),('CK'),('CL'),('CM'),('CN'),('CO'),('CR'),
 ('CU'),('CV'),('CW'),('CX'),('CY'),('CZ'),('DE'),('DJ'),('DK'),('DM'),
 ('DO'),('DZ'),('EC'),('EE'),('EG'),('EH'),('ER'),('ES'),('ET'),('FI'),
 ('FJ'),('FK'),('FM'),('FO'),('FR'),('GA'),('GB'),('GD'),('GE'),('GF'),
 ('GG'),('GH'),('GI'),('GL'),('GM'),('GN'),('GP'),('GQ'),('GR'),('GS'),
 ('GT'),('GU'),('GW'),('GY'),('HK'),('HM'),('HN'),('HR'),('HT'),('HU'),
 ('ID'),('IE'),('IL'),('IM'),('IN'),('IO'),('IQ'),('IR'),('IS'),('IT'),
 ('JE'),('JM'),('JO'),('JP'),('KE'),('KG'),('KH'),('KI'),('KM'),('KN'),
 ('KP'),('KR'),('KW'),('KY'),('KZ'),('LA'),('LB'),('LC'),('LI'),('LK'),
 ('LR'),('LS'),('LT'),('LU'),('LV'),('LY'),('MA'),('MC'),('MD'),('ME'),
 ('MF'),('MG'),('MH'),('MK'),('ML'),('MM'),('MN'),('MO'),('MP'),('MQ'),
 ('MR'),('MS'),('MT'),('MU'),('MV'),('MW'),('MX'),('MY'),('MZ'),('NA'),
 ('NC'),('NE'),('NF'),('NG'),('NI'),('NL'),('NO'),('NP'),('NR'),('NU'),
 ('NZ'),('OM'),('PA'),('PE'),('PF'),('PG'),('PH'),('PK'),('PL'),('PM'),
 ('PN'),('PR'),('PS'),('PT'),('PW'),('PY'),('QA'),('RE'),('RO'),('RS'),
 ('RU'),('RW'),('SA'),('SB'),('SC'),('SD'),('SE'),('SG'),('SH'),('SI'),
 ('SJ'),('SK'),('SL'),('SM'),('SN'),('SO'),('SR'),('SS'),('ST'),('SV'),
 ('SX'),('SY'),('SZ'),('TC'),('TD'),('TF'),('TG'),('TH'),('TJ'),('TK'),
 ('TL'),('TM'),('TN'),('TO'),('TR'),('TT'),('TV'),('TW'),('TZ'),('UA'),
 ('UG'),('UM'),('US'),('UY'),('UZ'),('VA'),('VC'),('VE'),('VG'),('VI'),
 ('VN'),('VU'),('WF'),('WS'),('YE'),('YT'),('ZA'),('ZM'),('ZW'),
 ('UK'),('EL'),('XK')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION valida_country() RETURNS trigger AS $$
BEGIN
  IF NEW.country IS NOT NULL THEN
    NEW.country := upper(trim(NEW.country));
    IF NOT EXISTS (SELECT 1 FROM iso_paesi WHERE code = NEW.country) THEN
      NEW.country := NULL;   -- non e' un paese vero: meglio vuoto che falso
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_valida_country ON ats_jobs;
CREATE TRIGGER trg_valida_country
  BEFORE INSERT OR UPDATE OF country ON ats_jobs
  FOR EACH ROW EXECUTE FUNCTION valida_country();
-- Il timbro all'INSERT filtrava dentro EXCLUDED negli upsert ON CONFLICT:
-- ogni rivisita ri-timbrava posted_at=now() sulle offerte senza data,
-- senza flag (misurato: 87k "pubblicate" in un'ora = il volume di
-- rivisita). Il trigger ora NON timbra piu': protegge soltanto —
-- un refetch senza data non cancella la data che abbiamo, e il flag
-- si spegne quando arriva la data vera. Il timbro onesto (prima vista)
-- lo mette un passo periodico usando created_at, che non si muove.
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS posted_at_estimated boolean NOT NULL DEFAULT false;

CREATE OR REPLACE FUNCTION riempi_posted_at() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF NEW.posted_at IS NULL THEN
      NEW.posted_at := OLD.posted_at;
      NEW.posted_at_estimated := OLD.posted_at_estimated;
    ELSIF OLD.posted_at_estimated
          AND NEW.posted_at IS DISTINCT FROM OLD.posted_at THEN
      NEW.posted_at_estimated := false;
    END IF;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_riempi_posted_at ON ats_jobs;
CREATE TRIGGER trg_riempi_posted_at
  BEFORE UPDATE OF posted_at ON ats_jobs
  FOR EACH ROW EXECUTE FUNCTION riempi_posted_at();
