# Nivult — Engine

Backend di Nivult (nivult.com): servizio in abbonamento che consegna a ogni utente un
digest ricorrente di offerte di lavoro selezionate da un'AI sulla base del suo CV.

**Questo repo contiene solo il motore.** Il sito pubblico vive in un altro repo.

## Dominio

**Cluster** = coppia *famiglia professionale × paese* (es. Human Resources × Italia).
I cluster sono condivisi tra utenti: se dieci utenti seguono HR in Italia, le offerte
si scaricano **una volta sola**. L'ingestione è per cluster, non per utente.

Le famiglie professionali sono quelle di `ai_taxonomies_a` del fornitore.
**Non costruiamo una tassonomia nostra.**

## Pipeline

### 1. Ingestione (giornaliera, per cluster attivo)

Fonti:

- **Fantastic.jobs** — API a pagamento. `GET https://data.fantastic.jobs/v1/active-ats`,
  auth `Bearer`, parametri `title`, `location`, `time_frame`, `limit`.
- **API pubbliche nazionali gratuite** — France Travail, Bundesagentur,
  Arbetsförmedlingen, NAV, Työmarkkinatori.

**Regola dura:** solo offerte da career site aziendali. Il link deve portare al sito
dell'azienda, mai a un intermediario/aggregatore. Se non è verificabile, l'offerta si scarta.

### 2. Matching a imbuto (per utente, per ciclo)

1. Filtri deterministici sui campi strutturati
2. Ricerca ibrida: embedding **BGE-M3** su pgvector + full-text Postgres, fusi con
   **Reciprocal Rank Fusion**
3. Reranker **bge-reranker-v2-m3**
4. Le ~15 offerte finaliste vanno a un LLM (**GLM 5.2** via API) che assegna un
   punteggio 0–100 e scrive una riga di motivazione

Passano solo le offerte **sopra soglia alta**. Meglio un digest vuoto che un digest scadente.

### 3. Consegna

Digest via **email**, **Telegram** o **WhatsApp**, alla frequenza scelta
dall'utente (giornaliera / settimanale / mensile).

Se nessuna offerta supera la soglia, **non si manda nulla**. Un digest vuoto è
un esito legittimo (`digests.status = 'skipped_empty'`), non un fallimento.

## Dati del fornitore (Fantastic.jobs)

**Sempre presenti (100%)** — si può fare affidamento su questi campi:

`id`, `url`, `title`, `organization`, `date_posted`, `source`, `locations`/`cities`/
`countries_derived`, `domain_derived`, `org_linkedin_slug`, e i campi AI:
`ai_job_language`, `ai_visa_sponsorship`, `ai_work_arrangement`, `ai_experience_level`,
`ai_employment_type`, `ai_working_hours`, `ai_key_skills` (lista), `ai_keywords` (lista),
`ai_taxonomies_a` (lista di famiglie professionali), `ai_requirements_summary`,
`ai_core_responsibilities`.

**Parziali — trattare sempre come opzionali (nullable, mai obbligatori in nessuna logica):**

| Campo | Copertura |
|---|---|
| `salary` | <30% |
| `date_valid_through` | 15% |
| `ai_education` | 76% |
| `organization_logo` | 40% |
| `ai_work_arrangement_office_days` | 16% |

## Infrastruttura

- VPS **Hetzner** (Helsinki), Ubuntu 24.04
- **Postgres 17 + pgvector** in Docker su `/opt/nivult`, in ascolto **solo su 127.0.0.1**
- Backup notturni automatici
- Stringa di connessione in `/opt/nivult/.env` **sul server** (non nel repo)

## Regole tecniche

- **Python.**
- **Migrazioni versionate** per lo schema. Mai modifiche manuali al database.
- **Tutto in UTC.** Il database è impostato su UTC; il fuso dell'utente serve
  solo a calcolare l'orario di invio.
- **Niente float sui soldi.** I costi sono interi in milionesimi (`cost_micros`).
- **Chi verifica che qualcosa sia stato scritto deve leggere da un'altra
  connessione.** Sulla stessa connessione si vede anche il non-committato: è
  così che il bug dei savepoint era passato inosservato. `check_modules.py` usa
  un testimone separato, in autocommit — senza autocommit resterebbe *idle in
  transaction* trattenendo lock, e il `TRUNCATE` di pulizia lo aspetterebbe per
  sempre.
- **I cicli a lotti vogliono una connessione dedicata.** `psycopg.Connection.transaction()`
  apre un SAVEPOINT invece di una transazione se una è già aperta: i lotti non
  verrebbero committati e un rollback del chiamante butterebbe via tutto in
  silenzio. `nivult.gdpr` e `nivult.retention` committano esplicitamente dopo
  ogni lotto e rifiutano una connessione con una transazione già in corso.
- **Nessuna chiave o password nel codice.** Solo variabili d'ambiente.
  `.env` sempre in `.gitignore`.
- **Conservare il JSON grezzo** di ogni offerta in una colonna `jsonb` accanto alle
  colonne estratte, così un domani non servono migrazioni per campi nuovi.
- **Il CV è un dato personale:** prevedere cancellazione su richiesta,
  e **non loggarne mai il contenuto**.
- **Budget crediti per cluster con circuit breaker:** nessun cluster può superare la
  sua quota giornaliera.

## Decisioni di architettura

Queste sono state decise e non vanno riaperte senza motivo.

### Il worker itera sui cluster, non sugli utenti

La personalizzazione comincia **dal reranker in poi**. Il ciclo è:

```
per ogni cluster attivo:
    calcola UNA VOLTA l'insieme candidato del cluster
      (filtri deterministici + ricerca ibrida BGE-M3/full-text + RRF)
    per ogni utente iscritto al cluster:
        applica i filtri personali
        reranker  ->  ~15 finaliste  ->  LLM
```

Il motivo è il fan-out: iterando sugli utenti, diecimila utenti fanno diecimila
ricerche ibride al giorno sullo stesso identico insieme di offerte. Iterando sui
cluster, la parte costosa si paga una volta per cluster.
Non scrivere la pipeline nell'altro verso "per ora che gli utenti sono pochi":
è il cuore del motore e riscriverlo dopo costa più che farlo giusto adesso.

### Ricerca vettoriale — misurato, non ipotizzato

**Il filtro per cluster impedisce del tutto l'uso dell'indice HNSW.** Misurato su
Postgres 17.11 + pgvector 0.8.6, 20.000 offerte, cluster al 5% del corpus:

| Forma della query | HNSW |
|---|---|
| `JOIN job_clusters` (filtro in un'altra tabella) | **no** — seq scan su tutti gli embedding |
| `WHERE job_id IN (subquery)` | **no** — identico al join |
| `WHERE EXISTS (...)` | **no** — identico al join |
| `WHERE cluster = ANY(cluster_ids)` (array denormalizzato) | **no** — l'array non si combina con l'ANN |
| `WHERE country = 'IT'` (scalare, stessa tabella) | **sì** |
| nessun filtro | **sì** |

Ne seguono due conseguenze.

**`hnsw.iterative_scan` non ci sta salvando da niente.** Resta attivo perché è
gratis e servirà, ma agisce solo *dentro* una scansione HNSW già scelta dal
planner — e col nostro filtro il planner non la sceglie mai. Non contarci come
rete di sicurezza: non lo è.

**Il problema non è la recall, è la seq scan.** Con il filtro a join Postgres
calcola la distanza esatta sull'insieme candidato: recall perfetta, e a questi
volumi è pure più veloce dell'ANN. Ciò che non scala è che per arrivarci scandisce
*tutti* gli embedding. A cinque milioni di offerte sono venti gigabyte letti per
ogni ricerca.

**Forma da usare nel worker.** Restringere prima, ordinare dopo: si ricavano gli
id candidati da `job_clusters` più i filtri deterministici, poi

```sql
SELECT job_id FROM job_embeddings
WHERE job_id = ANY($1::uuid[])
ORDER BY embedding <=> $2 LIMIT $3
```

che il planner risolve con un bitmap index scan sulla primary key — misurato:
500 righe lette, 5 blocchi di heap, nessuna seq scan. Costo proporzionale alla
dimensione del cluster e non a quella del corpus, con recall esatta.
Da **non** scrivere come CTE materializzata: quella ricade in seq scan (misurato).

**Piano futuro, non da implementare ora:** quando l'insieme candidato di un
singolo cluster diventerà troppo grande per la distanza esatta, partizionare
`job_embeddings` per paese. Il paese funziona perché è un **predicato scalare
sulla tabella indicizzata** — l'unica forma che il planner combina con l'HNSW — e
lì `iterative_scan` comincerà davvero a servire. La strada dell'array
`cluster_ids` denormalizzato è già stata provata e non funziona.
Il segnale per farlo: latenza della ricerca per cluster, non il numero di righe.

### `matches` non è partizionata, ed è deliberato

In Postgres una UNIQUE su tabella partizionata deve contenere tutte le colonne
della chiave di partizionamento. Partizionando per mese, l'unica UNIQUE ammessa
sarebbe `(user_id, job_id, evaluated_at)` — che **consente** ciò che dobbiamo
vietare: la stessa offerta rimandata allo stesso utente il mese dopo. Un vincolo
che sembra protettivo e non protegge è peggio di nessun vincolo.

**Migrazione futura**, quando la tabella diventerà ingombrante: scindere in
`matches` partizionata per mese (punteggio, motivazione, token — il materiale
pesante e potabile) più `user_job_seen (user_id, job_id, first_evaluated_at)`
non partizionata, che tiene l'anti-ripetizione. Stesso numero di righe ma molto
più stretta, quindi si può conservare per sempre mentre il testo si pota.

### Retention delle offerte

Offerte con status `expired` o `removed` da più di **60 giorni** spariscono,
jsonb grezzo compreso. **Le offerte `active` non scadono mai**, per vecchia che
sia `date_posted`.

Ma un'offerta già valutata o già inviata non può semplicemente sparire: la
`UNIQUE (user_id, job_id)` su `matches` è l'anti-ripetizione, e `digest_items` è
il registro di cosa un utente ha ricevuto. Quindi due livelli:

| Offerta morta | Trattamento |
|---|---|
| non referenziata (la grande maggioranza) | `DELETE`, con `job_clusters` e `job_embeddings` in cascata |
| referenziata da `matches` o `digest_items` | **lapide**: via `raw`, embedding, `tsv`, testi lunghi, liste; `purged_at` valorizzato |

In entrambi i casi il jsonb grezzo sparisce, ed è il grosso del volume.
Misurato su 3.000 offerte morte con payload realistico, di cui il 20% già
valutate: **da 41 MB a 1,4 MB, il 96%**, con tutti i match conservati.

`purged_at IS NULL` nel predicato dell'indice è anche la garanzia di idempotenza:
una lapide non viene ripresa al giro successivo.

**La lapide conserva `title`, `organization`, `url` e `date_posted`, e non è
negoziabile.** Sono ciò che un utente vede riaprendo un digest di mesi fa:
`digest_items` tiene punteggio e motivazione, ma di quale offerta si parlasse lo
dice solo la riga di `jobs`. Aggiungere uno di questi campi alla lista che
`purge_dead_jobs` azzera renderebbe illeggibile tutto lo storico delle consegne,
in silenzio. Un test in `check_modules.py` lo blocca.

**Prima di cancellare**, `purge_dead_jobs` aggiorna `cluster_month_stats`:
offerte per cluster per mese, con conteggi per stato, copertura del campo
`salary` e vita media. **Nessun riferimento a utenti, per costruzione** — quando
il corpus sparisce restano le statistiche, ma non chi ha visto cosa. I contatori
sono somme e conteggi, mai medie, così i lotti successivi si sommano.

Da mettere in cron una volta al giorno, dopo l'ingestione:
`python scripts/purge_jobs.py`.

### Cancellazione dell'utente

`nivult.gdpr` + `scripts/delete_user.py`. Cancella a lotti in transazioni brevi,
in ordine di dipendenza, invece di far scattare la cascata delle FK in un colpo
solo. `deletion_requests` sopravvive all'utente come prova di cancellazione, e
non contiene dati personali.

Le righe di `api_usage` non si cancellano: si mette a NULL `user_id`. Il costo
sostenuto resta contabilizzato, l'attribuzione a una persona no.

**Una richiesta non si chiude finché i file su object storage non sono stati
rimossi**, non solo le righe. Le chiavi vengono raccolte prima della
cancellazione e restano in `deletion_requests.pending_storage_keys`.

### Vincoli aperti sui dati del fornitore

- `user_clusters.company_sizes` esiste ma **il funnel non deve applicarlo**: la
  dimensione azienda non è fra i campi del fornitore. Servirebbe un arricchimento
  a partire da `org_linkedin_slug`.
- `experience_levels` contiene un vocabolario **provvisorio**, ipotizzato prima
  di aver visto una risposta reale di Fantastic.jobs. Va confermato alla prima
  ingestione. `scripts/verify_schema.py` segnala i valori fuori vocabolario:
  quelli non vengono considerati dai filtri di seniority.
- Il full-text usa la configurazione `'simple'` (nessuno stemming), perché le
  offerte sono multilingua. La semantica la porta BGE-M3. La colonna
  `jobs.text_search_config` e il trigger `jobs_derive_fields` sono già
  predisposti per passare a una configurazione per lingua.

## Comandi

```bash
python -m nivult.migrate status          # cosa è applicato e cosa manca
python -m nivult.migrate up --dry-run    # elenca senza applicare
python -m nivult.migrate up              # applica

python scripts/reset_and_verify.py       # azzera, riapplica tutto, verifica. Da rilanciare a ogni modifica.
python scripts/verify_schema.py          # struttura, sola lettura, sicuro in produzione
python scripts/check_constraints.py      # i vincoli rifiutano davvero? (solo db _test/_dev)
python scripts/check_modules.py          # lo strato Python committa davvero? (solo db _test/_dev)
python scripts/delete_user.py --user-id <uuid>
python scripts/purge_jobs.py --dry-run    # retention offerte morte
python scripts/purge_jobs.py --stats      # aggregati per cluster e mese
```

Le migrazioni sono file SQL numerati in `migrations/`, applicati in ordine da un
runner minimo. Niente Alembic: lo schema è pesantemente specifico di Postgres e
ogni migrazione sarebbe comunque `op.execute()` di SQL grezzo.
**Una migrazione già applicata non si modifica** — il runner confronta i
checksum e si rifiuta di proseguire. Se serve un cambiamento, nuova migrazione.

## Sicurezza

### Autenticazione: niente password

Magic-link via email, più Google e Microsoft come provider OAuth. **Non esiste
una colonna password** in nessuna tabella, e `verify_schema.py` fallisce se
qualcuno ne aggiunge una. Niente password significa niente reset da mettere in
sicurezza, niente credenziali riusate da altri siti, niente hash da difendere.

- `login_tokens` e `sessions` conservano **solo lo sha256** del token, mai il
  token. Se il database trapela, quello che si trova non permette di entrare.
  sha256 nudo è sufficiente: i token li generiamo noi con entropia alta, non
  sono password indovinabili, quindi una KDF costosa non aggiunge nulla.
- `oauth_identities` ha come chiave `(provider, subject)` e **non l'email**. Il
  claim `sub` è l'unico identificativo stabile: le email cambiano e certi domini
  le riassegnano, quindi agganciare l'account all'email significherebbe che chi
  eredita un indirizzo eredita l'account.
- Il token in chiaro non deve comparire nei log, mai — stessa regola del CV.

### CV: cifratura lato client, a busta

Object storage su **Hetzner Object Storage**, stesso data center del database,
dati in UE. Il file viene cifrato **prima** dell'upload: Hetzner riceve byte
opachi e non ha alcuna chiave.

Schema a busta: ogni CV ha la sua DEK casuale (AES-256-GCM); la DEK viene
avvolta con la KEK che sta in variabile d'ambiente sul server, e la DEK avvolta
finisce in `user_cvs.encrypted_dek`. La KEK non entra mai nel database.

Il motivo della busta è la rotazione: cambiare KEK vuol dire riavvolgere DEK di
poche decine di byte, non riscaricare e ricifrare ogni CV. `kek_version` dice
con quale generazione è avvolta ciascuna DEK, così la rotazione è incrementale
invece che atomica — nessuna finestra in cui metà dei CV è illeggibile.

### Backup

`deploy/backup.sh`, in cron alle 03:00. `pg_dumpall` (include i ruoli, che non
stanno dentro il database) → gzip → **cifratura a chiave pubblica** con
`openssl cms` → copia su Hetzner Storage Box.

**Sul server sta solo il certificato pubblico.** Quella macchina può produrre
backup ma non può rileggerli: se viene compromessa, l'attaccante non ottiene lo
storico. La chiave privata vive nel password manager e sul Mac, mai altrove.

Conseguenza da tenere a mente: **il server non può verificare i propri backup.**
Riesce a controllare che il file sia una struttura CMS valida e non sia
sospettosamente piccolo, ma che il contenuto sia ripristinabile lo può dire solo
un ripristino di prova fatto fuori, con la chiave privata. Va fatto ogni
trimestre. Un backup mai ripristinato non è un backup verificato.

Se la copia off-site non è configurata o fallisce, lo script **esce in errore**.
Un backup che vive solo sul disco del database non è un backup, è una copia, e
un off-site che smette in silenzio è il modo classico di scoprirlo troppo tardi.

Ripristino:

```bash
openssl cms -decrypt -inform DER -in nivult-AAAA-MM-GG.sql.gz.enc \
  -inkey nivult-backup-PRIVATE.pem | gunzip | psql -U postgres
```

### Server

Aggiornamenti automatici **solo dal canale security** (`52nivult-security`, con
`#clear` sulla lista ereditata: in apt.conf le liste si concatenano invece di
sostituirsi, quindi senza `#clear` l'archivio completo resta dentro). Riavvio
automatico alle 04:00, dopo la finestra di backup delle 03:00 — un riavvio a
metà dump lascerebbe un backup troncato, mentre a valle non perde lavoro perché
il runner di migrazioni e i job a lotti sono riprendibili.

Postgres ascolta solo su `127.0.0.1`. Dall'esterno si passa da un tunnel SSH.

### Inventario dei segreti e rotazione

| Segreto | Dove vive | Come si ruota | Cosa si rompe durante |
|---|---|---|---|
| Chiave privata backup | password manager + Mac. **Mai sul server** | nuova coppia, cert nuovo sul server; conservare la vecchia privata finché esistono backup cifrati con essa | niente: i backup nuovi usano la nuova, i vecchi la vecchia |
| KEK dei CV | env sul server + password manager | nuova KEK con `kek_version+1`, riavvolgere le DEK a lotti | niente, grazie a `kek_version` |
| Password Postgres | `docker-compose.yml` ⚠ e `/opt/nivult/.env` | `ALTER ROLE ... PASSWORD`, poi aggiornare `.env` e riavviare l'applicazione | connessioni attive cadono, il runner è riprendibile |
| Chiavi delle 6 fonti | `.env` sul server | rigenerare dal portale del fornitore, sostituire, riavviare | l'ingestione fallisce finché non è sostituita |
| Chiave GLM | `.env` sul server | come sopra | il matching si ferma allo stadio LLM |
| SMTP / Telegram / WhatsApp | `.env` sul server | come sopra | i digest non partono; `digests.status` va a `failed` e vengono ritentati |
| Client secret OAuth | `.env` sul server | dal portale Google/Microsoft, con periodo di sovrapposizione | nessuno, se si sovrappongono |
| Chiave SSH Storage Box | `/root/.ssh/id_ed25519_storagebox` | nuova chiave, installarla sulla Storage Box, rimuovere la vecchia | il backup fallisce rumorosamente |

**Se una chiave trapela alle 3 di notte:** revocare prima di sostituire — una
chiave ruotata ma non revocata resta valida. Poi sostituire in `.env`, riavviare,
e verificare dai log che il servizio sia ripartito. Per la KEK dei CV e la chiave
privata dei backup non c'è revoca possibile: lì l'unica risposta è ricifrare, e
per i backup significa considerare compromesso tutto lo storico cifrato con essa.

### Debiti aperti

- ⚠ **`POSTGRES_PASSWORD` è in chiaro in `docker-compose.yml`**, contro la regola
  "nessuna password nel codice". Va spostata in `.env` e referenziata come
  `${POSTGRES_PASSWORD}`. Richiede di ricreare il container.
- **Un solo ruolo Postgres, con privilegi di superutente.** Vanno separati
  `nivult_app` (solo DML) e `nivult_migrator` (DDL, usato solo dal runner).
  Attenzione ad `ALTER DEFAULT PRIVILEGES` per il migrator, altrimenti ogni
  migrazione nuova rende le tabelle inaccessibili all'applicazione.
- **Nessuna scansione dei segreti nei commit.** Hook pre-commit con `gitleaks`,
  più lo stesso controllo in CI: l'hook protegge solo la macchina dove è
  installato.
- **Ripristino di prova mai eseguito.** Da fare al primo trimestre.

## Metodo di lavoro

- **Prima di modifiche non banali, proponi il piano e aspetta conferma.**
- **Non over-ingegnerizzare:** la soluzione più semplice che funziona.
- **Ogni pezzo deve essere testabile da solo, da riga di comando.**
