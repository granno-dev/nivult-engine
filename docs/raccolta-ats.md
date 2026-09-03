# La raccolta ATS — come prendiamo le offerte alla fonte

Questo documento spiega, dall'inizio alla fine, come Nivult raccoglie le
offerte di lavoro direttamente dagli ATS (Applicant Tracking System) delle
aziende, con il **link diretto al datore** e senza rimbalzi di aggregatori.

## La strategia, in una riga

Prendiamo le offerte dalle **API JSON pubbliche** che ogni ATS espone —
le stesse che la pagina carriere dell'azienda usa per mostrarle. Nessun
login, nessun anti-bot, link diretto.

**Non siamo indietro rispetto al mercato: facciamo esattamente ciò che
fanno i fornitori a pagamento** (jobdataapi, Fantastic, gli attori Apify).
Ricerca del 04/09/2026, verificata: gli aggregatori professionali (a) leggono
le stesse API JSON pubbliche degli ATS, e (b) scoprono le aziende
enumerando l'indice di **Common Crawl** per i pattern dei board
(`boards.greenhouse.io/*`, `jobs.lever.co/*`, …). Sono i nostri due
meccanismi identici. I kit "pronti" spediscono ~1.990 board verificati;
noi ne abbiamo censiti **~70.000**. La differenza con chi ha di piu' non
e' il metodo — e' l'ampiezza (numero di ATS supportati e di tenant
scoperti), che si colma continuando a scrivere adapter e a far girare la
scoperta.

## Le quattro fasi

```
  SCOPERTA            CENSIMENTO         RACCOLTA           RAFFINAZIONE
  (chi esiste)   →    (ats_companies) →  (adapters) →       (classifica + paese)
  Common Crawl        70k aziende        API JSON ATS       famiglia + ISO2
  + Wayback CDX       per ~50 ATS        link diretto              │
                                                                    ▼
                                                              PONTE → motore
                                                       (solo cio' che una
                                                        ricerca utente chiede)
```

### 1. Scoperta — `scoperta_archivi.py`

Interroga l'indice URL di **Common Crawl** e la CDX di **Wayback** per i
pattern-host di ogni piattaforma, estrae i token azienda (lo slug fra host
e path), e li verifica contro l'API JSON del board, tenendo solo quelli
vivi. E' il metodo standard dell'industria. Gira come demone
(`nivult-scoperta`). Regola imparata: `showNumPages` va chiesto SENZA
`fl=`/`collapse=`, altrimenti torna «-».

### 2. Censimento — `ats_companies`

La tabella dei tenant scoperti: `(platform_id, slug)`, piu' `pub_key`
(In-recruiting), `wd_server`/`wd_instance` (Workday), `job_count`,
`last_fetch_at`, `country`. Le piattaforme stanno in `ats_platforms`
(`is_active` governa se il raccoglitore le visita).

### 3. Raccolta — `adapters.py` + `runner.py`

Ogni ATS ha un **adapter**: una classe che, dato lo slug, chiama l'API
pubblica e ritorna una lista di `AtsJob` (titolo, url diretto, sede,
paese, data, reparto). ~50 adapter registrati in `ADAPTERS`. Il
`runner.py` scarica in parallelo (fetch multithread, scritture DB
serializzate), con priorita': prima le **mai viste**, poi le attive piu'
stantie (come il polling di un aggregatore).

**Regola critica (bug risolto 04/09):** un fetch fallito (rete, slug
morto, piattaforma senza adapter) DEVE comunque segnare `last_fetch_at`.
Senza, l'azienda fallita restava in testa alla coda a priorita' e
bloccava all'infinito tutte le altre mai-viste dietro di lei — la coda
dava lotti «0 aziende». Ora si segna anche sul fallimento e la coda
avanza.

#### La sede: dove gli ATS la nascondono

Non tutti gli ATS mettono la sede nello stesso posto; alcuni la nascondono.
Regole per adapter (bug di «sede buttata» risolti 04/09):
- **jazzhr**: la sede e' nella colonna `resumator-job-location-column`
  della riga (`Houston, MS`) — prima si prendeva solo il titolo.
- **softgarden**: nel feed JSON, in `jobLocation.address`
  (citta'/regione/paese).
- **successfactors**: in `<span class="jobLocation">` («Citta, CC, CAP»),
  col reparto in `jobFacility`.
- **icims**: NON nella pagina di dettaglio (li c'e' solo l'indirizzo
  dell'azienda, sbagliato per la singola offerta) ma nella **card di
  ricerca** dei portali che server-rendono (`Job Locations US-KS-Wichita`).
  Copertura ~57%: il resto dei portali non la pubblica da nessuna parte.
- **workday**: quando la localita' e' «N Locations», la sede primaria sta
  nel path dell'URL (`/job/New-York-USA---Remote/...`), estratta da
  `arricchisci_workday`.

#### Adapter nuovi (04/09/2026)

- **personio** — feed XML `{slug}.jobs.personio.com/xml`.
- **recruiterbox** (Trakstar Hire) — la SPA e' Firebase, ma il feed RSS di
  sindacazione `{slug}.hire.trakstar.com/jobfeeds/{slug}` e' pubblico, con
  citta'/stato/paese nel namespace `job:`.
- **icims** — vista mobile server-rendered, paginata con `pr`.
- **zohorecruit** — le offerte sono nel blob JSON (HTML-escaped)
  dell'elemento `id="jobs"` in `/jobs/Careers` (data center eu/com/in).

Vedi la memoria `adapter-piattaforme-pending` per l'analisi delle
piattaforme non raccoglibili (hirevue = assessment, jobteaser = login
studenti, jobadder/beamery/vidcruiter = spazzatura della scoperta),
disattivate in `ats_platforms`.

### 4. Raffinazione

- **Classificazione** (`classificatore_veloce.py`): famiglia
  professionale, multilingue, via dizionari + codici ISCO/ROME/AF
  (indipendente dalla lingua). Demone `nivult-classifica`.
- **Paese** (`arricchisci.py` + `geografia.py`): vedi sotto. Demone
  `nivult-arricchisci`.

## L'arricchimento del paese

Un'offerta senza paese non si puo' filtrare ne' consegnare. Cinque passi,
dal piu' sicuro al piu' largo:

1. `--da-localita` — il paese scritto per esteso nel testo
   (`_paese_dal_testo`: «Deutschland» → DE).
2. `--francetravail` — il paese dal **codice dipartimento** francese
   (`31 - ...` → FR; 971-988 = DOM-TOM col loro ISO). Autorevole, perche'
   «...-sur-...» ingannava il geocoder.
3. `--da-geonames` — la citta' geocodificata offline via **geonamescache**
   (4,8M localita', `geografia.py::paese_da_localita`), per QUALSIASI
   paese.
4. `--workday` — la sede primaria dall'URL Workday delle «N Locations».
5. `--da-azienda` — il paese dominante dell'azienda per il residuo senza
   localita'; azzera cio' che non ha evidenza (un paese sbagliato e'
   peggio di nessuno). Salta Workday, che ha il suo passo.

Il resolver `geografia.py` e' delicato per non dare paesi assurdi: niente
codici di due lettere nudi (MH = Maharashtra ma anche Marshall), niente
preposizioni scambiate per citta' (`_STOP`: «sur», «les», «van»…), la
citta' **piu' popolosa** vince fra i pezzi, un nome-paese esplicito non
ambiguo vince per primo. Vedi la memoria `arricchimento-paese-geonames`.

## Il ponte e il motore

Il `ponte_ats.py` travasa dalla raffineria (`nivult_ats`) al motore
(`nivult`, tabella `jobs`) **solo le offerte che corrispondono a una
ricerca attiva** di un iscritto (famiglia + paese del cluster), con
dedup cross-fonte. Percio' il motore ha poche migliaia di offerte anche
se la raffineria ne ha centinaia di migliaia: il motore e' guidato dalla
**domanda** (le ricerche), la raffineria e' l'**offerta** che aspetta.

## I demoni (systemd)

- `nivult-scoperta` — scopre nuovi tenant dagli archivi.
- `nivult-scrape` — raccoglie a lotti, priorita' alle mai-viste.
- `nivult-classifica` — famiglia professionale, in loop.
- `nivult-arricchisci` — da-localita + francetravail + da-geonames +
  workday + phenom, in loop.
- Notturno (`ats-nightly.sh`): da-azienda, mantenimento (scadenza/dedup),
  ponte.
- `nivult-api` — sito + cruscotto.

Deploy: `scp` sul server `37.27.36.85:/opt/nivult/engine`, poi
`systemctl restart`. Ogni modifica ad un adapter richiede il **riavvio di
`nivult-scrape`** (tiene il codice in memoria): dimenticarlo fa girare il
demone col codice vecchio.

## Il cruscotto

`api/cruscotto.py` — pannello privato (OAuth murato su una email) con lo
stato del motore in tempo reale, offerte per fonte/paese/famiglia, la card
**«ATS pending»** (piattaforme censite senza adapter, da aggiungere), il
grafico interattivo dell'andamento (clic su un punto → fonti dietro il
numero) e la sezione iscritti. La query «ATS pending» fa join su
`ats_platforms` e richiede `ap.is_active`: l'utente DB dell'API
(`nivult_app`) ha bisogno di `SELECT` su quella tabella.

## Perche' non compriamo una fonte (jobdataapi & co.)

jobdataapi da' link diretti agli ATS e sede strutturata, ma: (a) costa
**$495+/mese**, (b) aggrega gli **stessi ATS che gia' raccogliamo gratis**,
(c) non risolve niente che non risolviamo da soli. Gli aggregatori
gratuiti (Adzuna, Careerjet, Arbeitnow) danno **link di rimbalzo** — contro
la nostra regola del link diretto. Verificato: la nostra infrastruttura e'
la loro. La compreremmo solo per smettere di mantenere adapter, quando ci
sara' fatturato a coprirla.
