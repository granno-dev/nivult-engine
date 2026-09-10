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

-- Il PARERE DI v1 anche dove la famiglia c'e' gia'. v1 la calcola comunque
-- per ogni annuncio che legge; buttarla via quando GLM ha gia' etichettato
-- costava un audit da tre ore ogni volta che serviva sapere se fidarsi di
-- GLM (09/09/2026: l'audit era fermo all'08 e il 41% delle famiglie del
-- dataset v2 e' rimasto senza contro-verifica). Salvarla costa due colonne
-- e rende l'accordo GLM+v1 sempre aggiornato e gratuito.
ALTER TABLE job_classifications ADD COLUMN IF NOT EXISTS v1_family TEXT;
ALTER TABLE job_classifications ADD COLUMN IF NOT EXISTS v1_conf REAL;
CREATE INDEX IF NOT EXISTS job_classifications_accordo_idx
    ON job_classifications (family) WHERE v1_family IS NOT NULL AND v1_family = family;
-- il ripasso dei pareri (`classifica_v1 --pareri`) pesca da qui: senza
-- indice ogni lotto scandiva 1,86 milioni di righe
CREATE INDEX IF NOT EXISTS job_classifications_senza_parere_idx
    ON job_classifications (job_id) WHERE v1_family IS NULL;

-- Il salario letto nel TESTO dell'annuncio, dove la fonte non lo dichiara.
-- `salary_da` dice da dove viene, e non e' un dettaglio: il campo
-- dichiarato dalla fonte e' esatto, il testo lo legge un lettore a regole
-- che sbaglia una volta su quattordici (92,8% misurato il 10/09/2026 sul
-- campo strutturato usato come verita' gratis). Chi mostrera' il salario
-- nel digest, e chi addestrera' un modello sopra, devono poterli
-- distinguere invece di trovarseli mescolati.
-- `salary_testo_at` marca l'offerta ANCHE quando non si trova niente: piu'
-- di un milione di annunci nominano lo stipendio senza scrivere una cifra,
-- e senza il marcatore ogni giro li rileggerebbe tutti.
-- `livelli_at`: il classificatore a dizionario marca le offerte GUARDATE,
-- non quelle riuscite. Il 64% dei titoli non e' classificabile col
-- dizionario, e restando senza famiglia tornavano nel lotto ogni ora.
-- `employment_type` mescolava due domande diverse: quante ore si lavora e
-- che natura ha il rapporto. Da `full_time` non si ricava se il posto e'
-- stabile, e «permanent» non esisteva fra i valori — il filtro «e' a tempo
-- indeterminato?» non aveva risposta. Peggio: il dato arrivava e si
-- buttava («CDI» diventava `full_time`). Due assi, due colonne.
--   orario: full_time | part_time
--   durata: permanent | fixed_term | internship | apprenticeship | freelance
-- `employment_type` resta dov'e': non si rompe nulla a valle.
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS orario TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS durata TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS livelli_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS salary_da TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS salary_testo_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ats_jobs_salario_da_leggere_idx
    ON ats_jobs (id)
 WHERE expired_at IS NULL AND salary_min IS NULL AND salary_testo_at IS NULL;

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

-- ── Letture veritiere e riparazione degli adapter (07/09/2026) ────────
-- Il 06/09 tre stragi di offerte vive avevano la stessa radice: una
-- lettura fallita o muta trattata come «bacheca vuota». Da qui:
--   last_ok_at        — l'ultima lettura RIUSCITA del tenant (offerte
--                       parseate, o bacheca vuota confermata). La scadenza
--                       per presenza si deduce solo da questa.
--   letture_sospette  — un adapter che torna zero su un tenant che aveva
--                       offerte; con il campione della pagina.
--   canarini          — 3 tenant di riferimento per piattaforma, riletti
--                       ogni ora: se tutti e tre danno zero, l'adapter e' rotto.
--   officina_riparazioni — cosa ha riparato Claude sul server, con esito.
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS last_ok_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS letture_sospette (
    id            BIGSERIAL PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    platform_id   TEXT NOT NULL,
    slug          TEXT NOT NULL,
    attive_prima  INTEGER NOT NULL,      -- offerte attive in archivio prima della lettura
    trovate       INTEGER NOT NULL,      -- cosa ha trovato l'adapter (0 = muto)
    confermate    INTEGER NOT NULL,      -- offerte d'archivio ritrovate dal ripiego nella pagina
    http_status   INTEGER,
    campione      TEXT                   -- percorso dell'HTML salvato, se salvato
);
CREATE INDEX IF NOT EXISTS letture_sospette_at_idx ON letture_sospette (at DESC);

CREATE TABLE IF NOT EXISTS canarini (
    platform_id   TEXT NOT NULL,
    slug          TEXT NOT NULL,
    attese        INTEGER NOT NULL,      -- offerte attive quando fu scelto
    scelto_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (platform_id, slug)
);

CREATE TABLE IF NOT EXISTS canarini_esiti (
    id            BIGSERIAL PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    platform_id   TEXT NOT NULL,
    slug          TEXT NOT NULL,
    trovate       INTEGER,               -- NULL = lettura fallita (rete, blocco)
    attese        INTEGER NOT NULL,
    campione      TEXT
);
CREATE INDEX IF NOT EXISTS canarini_esiti_at_idx ON canarini_esiti (at DESC);

CREATE TABLE IF NOT EXISTS officina_riparazioni (
    id            BIGSERIAL PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    platform_id   TEXT NOT NULL,
    motivo        TEXT NOT NULL,
    esito         TEXT NOT NULL,         -- 'deployata' | 'rollback' | 'bocciata' | 'fallita'
    commit        TEXT,
    dettaglio     TEXT,
    durata_s      INTEGER
);
GRANT SELECT ON letture_sospette, canarini, canarini_esiti, officina_riparazioni TO nivult_app;

-- ── Career page senza ATS (piattaforma «jsonld», 07/09/2026) ─────────
-- sorgente_url: la sitemap delle offerte o la pagina carriere da cui
-- l'adapter jsonld legge; jsonld_checked_at: quando la scoperta ha
-- provato quel dominio (si riprova dopo 30 giorni).
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS sorgente_url TEXT;
ALTER TABLE company_domains ADD COLUMN IF NOT EXISTS jsonld_checked_at TIMESTAMPTZ;
-- nivult-v1 (07/09/2026): marcatore proprio, separato da locale_at di v0, cosi' v1 rivede tutto.
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS locale_v1_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ats_jobs_locale_v1_idx ON ats_jobs (posted_at DESC) WHERE expired_at IS NULL AND locale_v1_at IS NULL;
-- campi dichiarati dall'ATS (07/09/2026): marcatore del passo nivult.ats.dichiarati
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS dichiarati_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS ats_jobs_dichiarati_idx ON ats_jobs (fetched_at DESC) WHERE expired_at IS NULL AND dichiarati_at IS NULL;
-- Le CHIMERE (07-08/09/2026): su iCIMS, Workday, Cornerstone, Eploy, Traffit,
-- Pinpoint, Vincere l'id dell'annuncio e' unico PER TENANT, ma la chiave era
-- (piattaforma, id): il 14145 di un tenant sovrascriveva titolo e URL del
-- 14145 di un altro. Misurato: iCIMS 30%, Cornerstone 36%, Eploy 57% delle
-- attive con l'URL di un altro tenant. La chiave giusta e' (piattaforma,
-- tenant, id). L'indice si costruisce fuori transazione (CONCURRENTLY) in
-- deploy/chimere.sh; qui si dichiara il vincolo se manca.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ats_jobs_platform_slug_external_key') THEN
    IF EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'ats_jobs_platform_slug_external_idx') THEN
      ALTER TABLE ats_jobs ADD CONSTRAINT ats_jobs_platform_slug_external_key UNIQUE USING INDEX ats_jobs_platform_slug_external_idx;
    ELSE
      ALTER TABLE ats_jobs ADD CONSTRAINT ats_jobs_platform_slug_external_key UNIQUE (platform_id, slug, external_id);
    END IF;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ats_jobs_platform_id_external_id_key') THEN
    ALTER TABLE ats_jobs DROP CONSTRAINT ats_jobs_platform_id_external_id_key;
  END IF;
END $$;
