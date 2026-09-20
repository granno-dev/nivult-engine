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


-- ===========================================================================
-- COLONNE AGGIUNTE STRADA FACENDO, rimesse nel documento il 19/09/2026.
--
-- Erano state create a mano o dai singoli moduli, e il file non le conosceva:
-- `expired_at` compresa, quella su cui filtra ogni query del motore. Un
-- database ricostruito da questo file non funzionava.
--
-- Stanno QUI, subito dopo le CREATE TABLE, perche' gli indici piu' sotto le
-- nominano: metterle in fondo avrebbe solo spostato l'errore.
-- ===========================================================================
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS logo_checked_at TIMESTAMPTZ;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS logo_domain TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS employees_wd INTEGER;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS industry_checked_at TIMESTAMPTZ;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS industry_reg TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS employees_reg INTEGER;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS employees_reg_band TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS reg_source TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS reg_checked_at TIMESTAMPTZ;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS industry_mix TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS employees_site INTEGER;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS industry_site TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_evidence TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_checked_at TIMESTAMPTZ;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_domain TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_domain_source TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS employees_self INTEGER;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS employees_self_n INTEGER;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS lei_source TEXT;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS lettura_parziale BOOLEAN;
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS dominio_cercato_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS expired_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS salary_min NUMERIC;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS salary_max NUMERIC;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS salary_currency TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS employment_type TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS normalized_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS duplicate_key TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS salary_period TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS seniority TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS remote TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS skills text[];
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS profiled_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS country_glm_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS salary_checked_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS lang TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS lang_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS contact_email TEXT;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS extra_checked_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS glm_extra_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS sprint_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS locale_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS languages_required text[];
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS lingue_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS estratto_2b_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS preso_2b_at TIMESTAMPTZ;
ALTER TABLE ats_platforms ADD COLUMN IF NOT EXISTS url_pattern TEXT;
ALTER TABLE ats_platforms ADD COLUMN IF NOT EXISTS api_endpoint TEXT;
ALTER TABLE ats_platforms ADD COLUMN IF NOT EXISTS cc_search TEXT;
ALTER TABLE ats_platforms ADD COLUMN IF NOT EXISTS market TEXT;
ALTER TABLE ats_platforms ADD COLUMN IF NOT EXISTS priority SMALLINT DEFAULT 3;
ALTER TABLE company_domains ADD COLUMN IF NOT EXISTS vanity_checked_at TIMESTAMPTZ;
ALTER TABLE company_domains ADD COLUMN IF NOT EXISTS lei_source TEXT;
ALTER TABLE iso_paesi ADD COLUMN IF NOT EXISTS code TEXT;

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


-- ===========================================================================
-- TABELLE RIMESSE NEL DOCUMENTO il 19/09/2026.
--
-- Erano create dai singoli moduli con un `prepara()`, e questo file — che
-- dovrebbe descrivere l'archivio — non le nominava. Fra loro `estrazioni_v2b`,
-- dove finiscono tecnologie e sintesi del 2B: meta' del prodotto.
-- Il DDL viene dalla produzione (pg_dump --schema-only), reso idempotente.
-- ===========================================================================
\restrict cnb3z8qft1TwyAgQ6mQnyqFgbjbwT59tdHfJaE9dPihnrNDjFK10noYOAefgKB2
CREATE TABLE IF NOT EXISTS azienda_paese (
    platform_id text NOT NULL,
    slug text NOT NULL,
    country text NOT NULL,
    jobs integer NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE IF NOT EXISTS azienda_skill (
    platform_id text NOT NULL,
    slug text NOT NULL,
    skill text NOT NULL,
    mentions integer NOT NULL,
    first_seen timestamp with time zone DEFAULT now() NOT NULL,
    last_seen timestamp with time zone DEFAULT now() NOT NULL,
    mentions_title integer DEFAULT 0 NOT NULL
);
CREATE TABLE IF NOT EXISTS aziende_pdl (
    dominio text NOT NULL,
    pdl_id text,
    nome text,
    fondata integer,
    fascia text,
    localita text,
    regione text,
    paese text,
    paese_nome text,
    settore text,
    linkedin_url text,
    fonte text NOT NULL,
    aggiornato timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE IF NOT EXISTS bilanci (
    lei text NOT NULL,
    periodo_fine date NOT NULL,
    fonte text NOT NULL,
    nome text,
    paese text,
    valuta text,
    ricavi numeric,
    utile numeric,
    attivo numeric,
    patrimonio numeric,
    dipendenti integer,
    periodo_inizio date,
    fxo_id text,
    fatti integer,
    aggiornato timestamp with time zone DEFAULT now() NOT NULL,
    dominio text
);
CREATE TABLE IF NOT EXISTS estrazioni_v2b (
    job_id uuid NOT NULL,
    tecnologie jsonb,
    sintesi text,
    modello text NOT NULL,
    testo_hash text,
    creato_at timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE IF NOT EXISTS etichette_deepseek (
    job_id uuid NOT NULL,
    family text,
    seniority text,
    employment_type text,
    remote text,
    settore text,
    citta text,
    regione text,
    paese text,
    sedi_multiple boolean,
    lingue_obbligatorie text[],
    lingue_gradite text[],
    tecnologie jsonb,
    n_tecnologie integer,
    salario_min numeric,
    salario_max numeric,
    salario_valuta text,
    salario_periodo text,
    sintesi text,
    sintesi_ok boolean,
    n_parole_sintesi integer,
    fonte jsonb,
    prove jsonb,
    modello text DEFAULT 'deepseek-flash'::text,
    giro text DEFAULT '200k-2026-09-14'::text,
    caricato_il timestamp with time zone DEFAULT now()
);
CREATE TABLE IF NOT EXISTS incidenti (
    id bigint NOT NULL,
    chiave text NOT NULL,
    gravita text NOT NULL,
    titolo text NOT NULL,
    dettaglio text,
    stato text DEFAULT 'aperto'::text NOT NULL,
    aperto_at timestamp with time zone DEFAULT now() NOT NULL,
    aggiornato_at timestamp with time zone DEFAULT now() NOT NULL,
    risolto_at timestamp with time zone,
    risolto_da text,
    cura text,
    telegram_id text,
    escalato boolean DEFAULT false NOT NULL
);
CREATE SEQUENCE incidenti_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE incidenti_id_seq OWNED BY incidenti.id;
CREATE TABLE IF NOT EXISTS medico_visite (
    id bigint NOT NULL,
    at timestamp with time zone DEFAULT now() NOT NULL,
    tipo text NOT NULL,
    motivo text NOT NULL,
    esito text,
    durata_s integer,
    rivista boolean DEFAULT false NOT NULL,
    CONSTRAINT medico_visite_tipo_check CHECK ((tipo = ANY (ARRAY['visita'::text, 'chat'::text, 'guardiano'::text, 'officina'::text])))
);
CREATE SEQUENCE medico_visite_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;
ALTER SEQUENCE medico_visite_id_seq OWNED BY medico_visite.id;
CREATE TABLE IF NOT EXISTS n5_vitali (
    at timestamp with time zone DEFAULT now() NOT NULL,
    mem_libera_mb integer,
    mem_totale_mb integer,
    carico numeric,
    processi_str text,
    processi_tot integer,
    descrittori integer,
    disco_root_mb integer,
    zombi integer,
    gpu_uso text,
    grossi text,
    gtt_usata_mb integer,
    gtt_totale_mb integer
);
CREATE TABLE IF NOT EXISTS operaio_battiti (
    nome text NOT NULL,
    battito timestamp with time zone NOT NULL,
    note text
);
CREATE TABLE IF NOT EXISTS radar_indeed (
    chiave text NOT NULL,
    tipo text NOT NULL,
    dominio text,
    platform_id text,
    slug text,
    azienda text,
    paese text,
    url_esempio text,
    viste integer DEFAULT 1 NOT NULL,
    primo_visto timestamp with time zone DEFAULT now() NOT NULL,
    ultimo_visto timestamp with time zone DEFAULT now() NOT NULL
);
CREATE TABLE IF NOT EXISTS sprint_coda (
    id uuid,
    ord bigint NOT NULL
);
CREATE TABLE IF NOT EXISTS stipendi_benchmark (
    country text NOT NULL,
    currency text NOT NULL,
    family text NOT NULL,
    seniority text DEFAULT ''::text NOT NULL,
    p25 integer NOT NULL,
    p50 integer NOT NULL,
    p75 integer NOT NULL,
    n integer NOT NULL,
    aggiornato timestamp with time zone DEFAULT now() NOT NULL
);
ALTER TABLE ONLY incidenti ALTER COLUMN id SET DEFAULT nextval('incidenti_id_seq'::regclass);
ALTER TABLE ONLY medico_visite ALTER COLUMN id SET DEFAULT nextval('medico_visite_id_seq'::regclass);
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'azienda_paese_pkey') THEN
    ALTER TABLE azienda_paese ADD CONSTRAINT azienda_paese_pkey PRIMARY KEY (platform_id, slug, country);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'azienda_skill_pkey') THEN
    ALTER TABLE azienda_skill ADD CONSTRAINT azienda_skill_pkey PRIMARY KEY (platform_id, slug, skill);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'aziende_pdl_pkey') THEN
    ALTER TABLE aziende_pdl ADD CONSTRAINT aziende_pdl_pkey PRIMARY KEY (dominio);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bilanci_pkey') THEN
    ALTER TABLE bilanci ADD CONSTRAINT bilanci_pkey PRIMARY KEY (lei, periodo_fine, fonte);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'estrazioni_v2b_pkey') THEN
    ALTER TABLE estrazioni_v2b ADD CONSTRAINT estrazioni_v2b_pkey PRIMARY KEY (job_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'etichette_deepseek_pkey') THEN
    ALTER TABLE etichette_deepseek ADD CONSTRAINT etichette_deepseek_pkey PRIMARY KEY (job_id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'incidenti_pkey') THEN
    ALTER TABLE incidenti ADD CONSTRAINT incidenti_pkey PRIMARY KEY (id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'medico_visite_pkey') THEN
    ALTER TABLE medico_visite ADD CONSTRAINT medico_visite_pkey PRIMARY KEY (id);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'operaio_battiti_pkey') THEN
    ALTER TABLE operaio_battiti ADD CONSTRAINT operaio_battiti_pkey PRIMARY KEY (nome);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'radar_indeed_pkey') THEN
    ALTER TABLE radar_indeed ADD CONSTRAINT radar_indeed_pkey PRIMARY KEY (chiave);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'sprint_coda_pkey') THEN
    ALTER TABLE sprint_coda ADD CONSTRAINT sprint_coda_pkey PRIMARY KEY (ord);
  END IF;
END $$;
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'stipendi_benchmark_pkey') THEN
    ALTER TABLE stipendi_benchmark ADD CONSTRAINT stipendi_benchmark_pkey PRIMARY KEY (country, currency, family, seniority);
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS aziende_pdl_paese_idx ON aziende_pdl USING btree (paese);
CREATE INDEX IF NOT EXISTS bilanci_paese_idx ON bilanci USING btree (paese);
CREATE INDEX IF NOT EXISTS estrazioni_v2b_creato ON estrazioni_v2b USING btree (creato_at);
CREATE INDEX IF NOT EXISTS estrazioni_v2b_hash ON estrazioni_v2b USING btree (testo_hash);
CREATE INDEX IF NOT EXISTS etichette_deepseek_family ON etichette_deepseek USING btree (family);
CREATE INDEX IF NOT EXISTS etichette_deepseek_tec ON etichette_deepseek USING gin (tecnologie);
CREATE INDEX IF NOT EXISTS incidenti_aperti_idx ON incidenti USING btree (chiave) WHERE (stato <> 'risolto'::text);
CREATE INDEX IF NOT EXISTS n5_vitali_at_idx ON n5_vitali USING btree (at DESC);
\unrestrict cnb3z8qft1TwyAgQ6mQnyqFgbjbwT59tdHfJaE9dPihnrNDjFK10noYOAefgKB2

-- ===========================================================================
-- 19/09/2026 — Le sintesi di mT5, il livello dei domini, la tabella aziende.
--
-- Tre pezzi che nascono insieme perche' rispondono alla stessa domanda: cosa
-- vendiamo, e quanto e' solido. Il mercato non compra annunci, compra AZIENDE
-- (TheirStack fa pagare un credito per ogni azienda rivelata), e un'azienda
-- senza dominio non si aggancia a niente.
-- ===========================================================================

-- --- LE SINTESI DI mT5 ------------------------------------------------------
-- Il 2B scriveva sintesi e tecnologie insieme, e il 93% di quello che scriveva
-- era la sintesi. Togliergliela lo rende 2,5 volte piu' veloce sulle tecnologie,
-- e le sintesi le fa mT5 sul N5: 95.000 al giorno contro 49.000.
-- Marcatori gemelli di estratto_2b_at / preso_2b_at.
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS sintesi_mt5_at TIMESTAMPTZ;
ALTER TABLE ats_jobs ADD COLUMN IF NOT EXISTS preso_mt5_at   TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS sintesi_mt5 (
    job_id            UUID PRIMARY KEY,
    sintesi           TEXT,
    -- La media del log-prob delle parole scelte: quanto il modello ci credeva.
    -- Misurata il 18/09 su 250 sintesi giudicate una per una, correla +0,472 col
    -- voto del giudice — le sintesi in cui mT5 esita sono davvero quelle che
    -- sbaglia. Serve al RIPASSO: il 2B riscrive quelle sotto soglia.
    fiducia           REAL,
    modello           TEXT NOT NULL,
    testo_hash        TEXT,
    -- la sintesi originale di mT5, conservata quando il 2B la riscrive
    sintesi_originale TEXT,
    ripassata_at      TIMESTAMPTZ,
    presa_ripasso_at  TIMESTAMPTZ,
    creato_at         TIMESTAMPTZ NOT NULL DEFAULT now());

-- Stesso ordine e stesso WHERE della query che prenota, o l'indice non viene
-- usato: il 18/09 un (posted_at DESC) contro una query DESC NULLS LAST costava
-- 131 secondi invece di 177 millisecondi.
CREATE INDEX IF NOT EXISTS ats_jobs_mt5_coda_idx
    ON ats_jobs (posted_at DESC NULLS LAST)
    WHERE expired_at IS NULL AND sintesi_mt5_at IS NULL;
CREATE INDEX IF NOT EXISTS ats_jobs_mt5_prenotate_idx
    ON ats_jobs (preso_mt5_at) WHERE preso_mt5_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS sintesi_mt5_ripasso_idx
    ON sintesi_mt5 (fiducia) WHERE ripassata_at IS NULL AND sintesi IS NOT NULL;
CREATE INDEX IF NOT EXISTS sintesi_mt5_hash_idx ON sintesi_mt5 (testo_hash);

-- Da dove si legge LA sintesi di un'offerta, adesso che i modelli sono due.
-- Vince il 2B quando c'e': o perche' l'ha scritta lui, o perche' ha RIPASSATO
-- quella di mT5 — ed e' proprio il caso in cui mT5 aveva esitato.
CREATE OR REPLACE VIEW sintesi_finali AS
SELECT j.id AS job_id,
       coalesce(CASE WHEN m.ripassata_at IS NOT NULL THEN m.sintesi END,
                e.sintesi, m.sintesi)                  AS sintesi,
       CASE WHEN m.ripassata_at IS NOT NULL AND m.sintesi IS NOT NULL THEN 'nivult-2b+ripasso'
            WHEN e.sintesi IS NOT NULL                              THEN 'nivult-2b'
            WHEN m.sintesi IS NOT NULL                              THEN 'nivult-mt5'
       END                                             AS da,
       m.fiducia                                       AS fiducia_mt5,
       m.ripassata_at                                  AS ripassata_at,
       m.sintesi_originale                             AS sintesi_mt5_originale
  FROM ats_jobs j
  LEFT JOIN estrazioni_v2b e ON e.job_id = j.id
  LEFT JOIN sintesi_mt5   m ON m.job_id = j.id
 WHERE coalesce(e.sintesi, m.sintesi) IS NOT NULL;

-- --- QUANTO E' SOLIDO UN DOMINIO -------------------------------------------
--  1  il sito rimanda al NOSTRO tenant ATS. E' una prova, e prende anche cio'
--     che il nome non direbbe mai: Orbotech -> kla.com (acquisita).
--  2  il dominio CORRISPONDE al nome. Serve perche' CVS Health, Broadcom e
--     Colliers un dominio ovvio ce l'hanno ma non mettono da nessuna parte un
--     link crawlabile al loro tenant.
-- Il livello si DICHIARA: chi compra sceglie fra copertura e certezza.
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS site_domain_livello SMALLINT;
-- da dove viene il nome: 'ats' se l'ha dato la piattaforma, 'dichiarato' se
-- l'abbiamo letto dal JSON-LD dell'annuncio. Mai una nostra invenzione muta.
ALTER TABLE ats_companies ADD COLUMN IF NOT EXISTS company_name_source TEXT;
CREATE INDEX IF NOT EXISTS ats_companies_livello_idx
    ON ats_companies (site_domain_livello) WHERE site_domain IS NOT NULL;

-- Quanto rende il cacciatore su ogni piattaforma. Serve all'ORDINE di pesca:
-- Vincere rende il 67%, Zoho il 52%, Ashby il 3% — a parita' di tempo provare
-- prima Zoho che Ashby vale diciassette volte tanto. Si ricalcola dai dati a
-- ogni giro del demone, non si scrive a mano.
CREATE TABLE IF NOT EXISTS caccia_resa (
    platform_id   TEXT PRIMARY KEY,    -- TEXT come in tutto lo schema ATS
    provate       INTEGER NOT NULL,
    trovate       INTEGER NOT NULL,
    resa          REAL,                -- NULL finche' le prove sono troppo poche
    aggiornato_at TIMESTAMPTZ NOT NULL DEFAULT now());

-- --- LA TABELLA CHE SI VENDE ------------------------------------------------
-- E' una TABELLA, non una vista: aggregare le tecnologie di 2,6 milioni di
-- offerte a ogni interrogazione costerebbe minuti. La ricostruisce
-- scripts/costruisci_aziende.py.
CREATE TABLE IF NOT EXISTS aziende_vendibili (
    company_id     UUID PRIMARY KEY,
    nome           TEXT,
    nome_fonte     TEXT,
    piattaforma    TEXT,
    slug           TEXT,
    -- l'aggancio al mondo reale: senza dominio l'azienda non vale niente
    dominio        TEXT,
    dominio_fonte  TEXT,
    dominio_livello SMALLINT,
    -- `carrieres-mousquetaires.com` invece di `mousquetaires.com`: l'aggancio
    -- regge, ma chi incrocia col CRM vuole il dominio aziendale. Si segnala.
    dominio_e_carriere BOOLEAN NOT NULL DEFAULT false,
    paese          TEXT,
    settore        TEXT,
    dipendenti     TEXT,
    lei            TEXT,
    offerte_attive INTEGER NOT NULL DEFAULT 0,
    ultima_offerta TIMESTAMPTZ,
    offerte_lette  INTEGER NOT NULL DEFAULT 0,   -- quante il 2B ha davvero letto
    tecnologie     JSONB,                        -- [{nome, offerte}] dalla piu' citata
    n_tecnologie   INTEGER NOT NULL DEFAULT 0,
    -- quanti nomi di datori DIVERSI compaiono nelle sue offerte: 1 = azienda,
    -- centinaia = bacheca. mindpal.co ne dichiara 769.
    nomi_dichiarati INTEGER,
    e_bacheca      BOOLEAN NOT NULL DEFAULT false,
    -- il capofila del gruppo: piu' tenant della stessa azienda puntano alla
    -- stessa riga. Non si cancella niente, si dice solo chi rappresenta chi.
    canonico_id    UUID,
    aggiornato_at  TIMESTAMPTZ NOT NULL DEFAULT now());

CREATE INDEX IF NOT EXISTS aziende_vendibili_dominio_idx  ON aziende_vendibili (dominio)  WHERE dominio IS NOT NULL;
CREATE INDEX IF NOT EXISTS aziende_vendibili_paese_idx    ON aziende_vendibili (paese);
CREATE INDEX IF NOT EXISTS aziende_vendibili_settore_idx  ON aziende_vendibili (settore);
CREATE INDEX IF NOT EXISTS aziende_vendibili_tec_idx      ON aziende_vendibili USING gin (tecnologie);
CREATE INDEX IF NOT EXISTS aziende_vendibili_canonico_idx ON aziende_vendibili (canonico_id);
CREATE INDEX IF NOT EXISTS aziende_vendibili_bacheca_idx  ON aziende_vendibili (e_bacheca) WHERE e_bacheca;

-- Cio' che si vende davvero: un'azienda per riga, niente bacheche, un nome.
CREATE OR REPLACE VIEW aziende_pronte AS
SELECT company_id, nome, nome_fonte, piattaforma, slug,
       dominio, dominio_fonte, dominio_livello, dominio_e_carriere,
       paese, settore, dipendenti, lei,
       offerte_attive, ultima_offerta, offerte_lette,
       tecnologie, n_tecnologie, nomi_dichiarati, canonico_id, aggiornato_at
  FROM aziende_vendibili
 WHERE NOT e_bacheca AND nome IS NOT NULL
   AND (canonico_id IS NULL OR canonico_id = company_id);
-- le ultime cinque, aggiunte a mano il 19/09/2026: il rilevatore automatico le
-- aveva credute presenti perche' quei nomi compaiono nel blocco di un'ALTRA
-- tabella (`industry` sta in company_domains, non in ats_companies).
ALTER TABLE ats_companies  ADD COLUMN IF NOT EXISTS industry TEXT;
ALTER TABLE ats_companies  ADD COLUMN IF NOT EXISTS lei TEXT;
ALTER TABLE ats_companies  ADD COLUMN IF NOT EXISTS logo_url TEXT;
ALTER TABLE ats_jobs       ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();
ALTER TABLE company_domains ADD COLUMN IF NOT EXISTS lei TEXT;

