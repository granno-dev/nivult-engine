# Nivult — Engine

Backend di Nivult (nivult.com): servizio in abbonamento che consegna a ogni utente un
digest ricorrente di offerte di lavoro selezionate da un'AI sulla base del suo CV.

**Questo repo contiene solo il motore.** Il sito pubblico vive in un altro repo.

## Dominio

**Cluster** = coppia *famiglia professionale × paese* (es. Human Resources × Italia).
I cluster sono condivisi tra utenti: se dieci utenti seguono HR in Italia, le offerte
si scaricano **una volta sola**. L'ingestione è per cluster, non per utente.

> **Un cluster è sempre famiglia × paese, mai un paese intero.** Non è una
> preferenza di stile: è il vincolo che tiene in piedi il budget. Un cluster
> stretto produce una decina di offerte al giorno, e le 20.000 mensili di
> Fantastic sono ~666 al giorno su tutti i cluster — margine ampio. «Tutta la
> Germania» ne fa 3.500 al giorno da solo e brucia la quota in meno di una
> settimana.
>
> Il rischio sui crediti non è mai il piano: è un cluster definito male.
> `daily_credit_cap` è tarato su **200**, abbondante per un cluster stretto e
> abbastanza stretto da scattare prima dei danni su uno largo. Un cluster che
> ne chiede molti di più va guardato, non aumentato.
> `verify_schema.py` segnala i cluster con più di 2000 offerte.

Le famiglie professionali sono quelle di `ai_taxonomies_a` del fornitore.
**Non costruiamo una tassonomia nostra.**

## Pipeline

### 1. Ingestione (giornaliera, per cluster attivo)

Fonti:

- **Fantastic.jobs** — API a pagamento. `GET https://data.fantastic.jobs/v1/active-ats`,
  auth `Bearer`, parametri `title`, `location`, `time_frame`, `limit`.
- **API pubbliche nazionali gratuite** — France Travail, Arbetsförmedlingen,
  NAV, Työmarkkinatori.

> **La Bundesagentur für Arbeit è fuori, deliberatamente.** Non offre una API
> pubblica: la vecchia chiave nota risponde `403`, e quello che circola come
> "API della Bundesagentur" sono credenziali OAuth2 estratte per reverse
> engineering dalla loro app mobile. Usare credenziali di qualcun altro per far
> funzionare un servizio in abbonamento è sbagliato su due piani — contrattuale,
> perché non è un accesso autorizzato; e operativo, perché possono essere
> ruotate da un giorno all'altro lasciandoci senza una fonte in produzione e
> senza nessuno a cui rivolgerci.
>
> **La Germania si copre con Fantastic.jobs** (~107.000 offerte ATS al mese su
> quel mercato). È stata inoltrata una richiesta di accesso partner autorizzato
> alla BA: se arriva, si valuta; se non arriva, non cambia niente.
>
> *Se qualcuno in futuro propone di rimettere dentro la Bundesagentur: la
> ragione dell'esclusione non è tecnica, e non è cambiata solo perché il codice
> funzionerebbe.*

**Regola sui link.** La regola nasce contro gli **aggregatori commerciali** che
rivendono traffico, non contro gli enti pubblici del lavoro. Quindi ogni offerta
porta un `link_kind`:

| `link_kind` | Ammesso | Nel digest |
|---|---|---|
| `career_site` | sì | «candidatura diretta» |
| `national_agency` | sì | «via France Travail», «via Bundesagentur», … |
| `job_board` | per ora no | — |

Due vincoli sul funnel, che lo schema supporta ma che vanno applicati a valle:

1. **A parità di punteggio, `career_site` viene prima.** L'ordine sta in
   `link_kinds.rank`, non in codice: è una politica di prodotto e cambiarla non
   deve richiedere un rilascio.
2. **Il digest deve mostrare l'etichetta.** La trasparenza è ciò che permette di
   ammettere le agenzie senza tradire la promessa. `link_kinds.is_direct` dice
   quale delle due formule usare.

`jobs.link_kind` è `NOT NULL` **senza default**: un default silenzioso
trasformerebbe una svista in una bugia all'utente.

> Il copy del sito pubblico va allineato. Sta nell'altro repo.

### 2. Matching — valutazione diretta, non a imbuto

**GLM 5.2 valuta direttamente tutte le offerte del cluster.** Niente embedding,
niente reranker, niente ricerca ibrida: l'imbuto è stato tolto perché il
giudizio del modello costa poco abbastanza da poterlo dare a tutte.

Quattro cose lo rendono sostenibile (~2,50 $/mese per un Ultra su cluster
stretti):

1. **thinking OFF** — su una valutazione a rubrica il ragionamento esteso
   moltiplica i token di uscita senza cambiare il punteggio;
2. **riassunto e competenze**, non la descrizione integrale, che è in gran parte
   boilerplate legale e di employer branding;
3. **CV e rubrica in cache**, quindi in testa a ogni chiamata e identici — se
   cambiassero fra un lotto e l'altro il risparmio sparirebbe;
4. **due passate**: punteggio secco su tutte, motivazione solo per le prime 30.

Passano solo le offerte **sopra soglia alta**. Meglio un digest vuoto che un
digest scadente.

**Due protezioni obbligatorie**, entrambe con la forma dei budget che già
usiamo — configurazione a parte, una riga per periodo, nessun azzeramento:

- **Budget di valutazione per utente**, legato al piano (`plan_quotas`,
  `user_evaluation_budget`, `user_try_evaluate`). Il costo qui è **per utente**,
  non per cluster: il corpus è condiviso ma il giudizio no, quindi cento utenti
  sullo stesso cluster costano cento volte.
- **Valvola per i cluster sovradimensionati.** Sopra
  `clusters.prescreen_threshold` offerte al mese entra un pre-screening con
  Mistral Small, che passa a GLM solo le migliori. È **per cluster**, non
  globale: attivarla ovunque pagherebbe un secondo modello dove non serve, e
  aggiungerebbe un anello che può sbagliare là dove GLM ce la fa da solo.
  Misurato: un cluster stretto fa ~300 offerte al mese, HR Germania 2.600, e
  HR Francia in produzione è già a 2.552.

> **Da decidere:** `job_embeddings`, l'indice HNSW, `user_cvs.embedding` e
> `jobs.tsv` non servono più a nessuno. Toglierli semplifica parecchio, ma è una
> porta che si richiude con difficoltà. Restano finché non lo decidiamo.

### 3. Consegna

Digest via **email**, **Telegram** o **WhatsApp**, alla frequenza scelta
dall'utente (giornaliera / settimanale / mensile).

Se nessuna offerta supera la soglia, **non si manda nulla**. Un digest vuoto è
un esito legittimo (`digests.status = 'skipped_empty'`), non un fallimento.

Il worker (`nivult.matching.worker`, orario via `deploy/digests.sh`) itera sugli
**utenti dovuti**, non sui cluster: il corpus è condiviso, il giudizio no. È
tutto riprendibile — un'offerta già valutata non si ripaga, un match passato e
mai spedito viene recuperato dal digest successivo — e il consumo del budget di
valutazione è per offerta, non anticipato: un errore a metà digest non butta via
la dotazione. `jobs_evaluated_count` conta le valutazioni che alimentano il
digest (quelle del run più le recuperate), non solo quelle appena pagate.

**La lingua del digest si GENERA, non si traduce.** Il sito parla inglese e
italiano (`nivult.com` e `nivult.com/it`), ma qui manca il pezzo: non esiste
una colonna lingua sull'utente — `user_cvs.languages` sono le lingue che il
*candidato parla*, estratte dal CV, un'altra cosa. Serve `users.locale`, e va
letta in due posti: il modello dell'email e **il prompt di GLM**.

Chiedere a GLM di scrivere la motivazione direttamente nella lingua dell'utente
costa **zero** — è una riga di istruzione, non una seconda chiamata, e non tocca
il prefisso in cache. Tradurla a valle costerebbe una chiamata per riga e
suonerebbe tradotta: quella riga *è* il prodotto.

**Gli annunci restano nella loro lingua.** Titolo, azienda e città arrivano
dall'offerta e non si toccano: ci si candida nella lingua in cui l'annuncio è
scritto, e tradurre il titolo renderebbe l'offerta irrintracciabile sul sito
dell'azienda.

Finché `users.locale` non c'è, **scegliere italiano sul sito e ricevere la mail
in inglese è peggio che non tradurre affatto**: è il debito che blocca la spinta
su `/it`.

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
- **Due connessioni, due ruoli.** `DATABASE_URL` è l'applicazione
  (`nivult_app`, solo DML); `MIGRATOR_DATABASE_URL` è il runner di migrazioni
  (`nivult_migrator`, con DDL). In sviluppo la seconda ricade sulla prima,
  perché c'è un ruolo solo.
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

### Ingestione

- **L'appartenenza al cluster viene dalla query che ha trovato l'offerta**, non
  da un campo dell'offerta. Le fonti nazionali non hanno `ai_taxonomies_a` — usano
  ROME e la classificazione della Bundesagentur — e non costruiamo una mappatura:
  interroghiamo la fonte coi termini del cluster e ciò che torna appartiene a
  quel cluster. È già il verso in cui lavora il worker cluster-first.
- **I campi AI mancanti si riempiono a regole, mai con un LLM.** France Travail dà
  `experienceExige` e `typeContrat`, e da lì si derivano `ai_experience_level` e
  `ai_employment_type`. Dove non c'è equivalente si lascia `NULL`: meglio un campo
  vuoto che un valore inventato su cui poi si filtra.
- **Quando arriveremo al funnel, un filtro su campo `NULL` dev'essere una
  decisione esplicita**, non un effetto collaterale.
- **La canonicalizzazione degli URL usa una denylist di parametri**, non una
  allowlist. Moltissimi career site identificano l'offerta proprio con un
  parametro di query (`?jobId=`, `?gh_jid=`): una allowlist li ridurrebbe tutti
  allo stesso `canonical_url`, che essendo UNIQUE significa scartarli come
  duplicati. `check_urls.py` blocca questo caso.
- **Gli scarti in normalizzazione si contano e si riportano.** `FetchResult.skipped`
  arriva fino al riepilogo del runner. Una perdita silenziosa in ingestione non
  si nota finché qualcuno non conta a mano, e a quel punto va avanti da
  settimane. È così che sono emersi gli URL senza schema.
- **`canonicalize` ripara gli URL senza schema** (`www.acme.se` →
  `https://www.acme.se`), ma solo se ciò che resta è un nome di dominio
  plausibile. I datori li scrivono così, e rifiutarli perdeva offerte vere.
- **La paginazione la guida il runner, non il client.** Il tetto giornaliero sta
  nel database e i client devono restare senza database, o il probe smette di
  funzionare. Il runner chiede `cluster_try_consume` prima di **ogni** pagina e
  registra ogni pagina in `api_usage`: una fonte che raddoppia le chiamate senza
  raddoppiare i risultati va vista subito.
- **L'offset avanza sui record RICEVUTI, non su quelli normalizzati.** Contando
  solo i normalizzati, ogni scarto sposterebbe la finestra e salterebbe
  un'offerta buona a ogni pagina.
- **`fetch_complete` è vero solo se l'ultima pagina ha esaurito i risultati E il
  ciclo non si è fermato per altro.** Ci si può fermare per tre motivi: tetto
  giornaliero, limite di scorrimento della fonte (France Travail si ferma a
  3149), rete di sicurezza sul numero di pagine. In tutti e tre la fetch è
  troncata, e da una fetch troncata le scadute non si deducono.
- **La deduplica morbida gira DOPO tutti i cluster, mai durante.** Un'offerta
  arrivata da un cluster può essere il duplicato di una arrivata da un altro, e
  deciderlo a metà strada sceglierebbe l'originale in base a chi è passato
  prima. Vince il `link_kind` più basso di rank, poi chi è arrivato prima, poi
  l'id: senza uno spareggio deterministico due esecuzioni sugli stessi dati
  potrebbero scegliere originali diversi.
- **Le offerte senza datore dichiarato sono escluse dalla deduplica morbida.**
  Senza `organization` il fingerprint resta titolo + città + settimana, e due
  aziende diverse che entrambe nascondono il nome collasserebbero in una. Il
  caso si è presentato al primo campione reale.
- **Lo sweep delle scadute richiede `ingestion_runs.fetch_complete`.** Se una
  fetch è stata troncata dal limite di pagina, l'assenza di un'offerta non
  significa che sia scaduta: significa che non siamo arrivati a leggerla.
- **Il backfill è di due settimane e ha una dotazione sua.** Un cluster mai
  scaricato prende 14 giorni di storico — non un mese: **un'offerta di tre
  settimane fa è spesso già chiusa, e un primo digest pieno di annunci morti è
  il peggior inizio possibile.** La dotazione è separata dal tetto giornaliero
  (`clusters.backfill_*`), altrimenti il primo giro di ogni cluster nuovo
  aprirebbe il breaker e resterebbe a metà.

  **Non è un'esenzione dai soldi:** `provider_budget` continua a valere. Il
  backfill è esente solo dal tetto giornaliero del cluster, che serve a
  un'altra cosa — accorgersi di una query impazzita.

  Si chiude da solo quando ha visto tutto, oppure quando la dotazione finisce:
  nel secondo caso `backfill_truncated` resta a `true` e `verify_schema.py` lo
  segnala, perché quel cluster parte con uno storico incompleto e va saputo.

  ⚠ **Su Fantastic 14 giorni costano come 30.** Gli scaglioni di `time_frame`
  sono `1h/24h/7d/1m/6m`: non esiste "due settimane", quindi si chiede `1m` e
  si scarta il resto — ma i crediti si pagano **per offerta restituita**, non
  per offerta tenuta. Da decidere quando accenderemo Fantastic: o si accetta di
  pagare il doppio del backfill, o per quella fonte si usa `7d`.
- **Due budget, non uno.** `cluster_daily_budget` protegge dalla singola query
  impazzita; `provider_budget` protegge la fattura. Sono cose diverse: venti
  cluster ciascuno entro il proprio tetto giornaliero possono esaurire le 20.000
  offerte mensili di Fantastic in pochi giorni senza che nessun breaker per
  cluster scatti. Le quote stanno in `provider_quotas`; un tetto a `0` significa
  fonte gratuita, contabilizzata ma mai bloccante.
- **Su Fantastic il costo non è noto prima della chiamata.** Scala **un credito
  per offerta restituita**, non per richiesta. Il ciclo è
  **riserva-poi-concilia**: si riserva il caso peggiore (la dimensione di
  pagina), si chiama, e `settle_credits` restituisce la differenza. Il verso
  conta — riservare poco e aggiustare in su lascerebbe una finestra in cui due
  worker paralleli sforano entrambi credendo di stare nel tetto.
- **Il cluster è una famiglia, le fonti lo interrogano ciascuna a modo suo.**
  `clusters.family` è un valore di `job_families` (la tassonomia del fornitore),
  con FK: **non è un termine di ricerca**. I termini per le fonti che la
  tassonomia non ce l'hanno stanno in `cluster_source_queries`, uno per fonte.
  Fantastic non ne ha bisogno: chiede `ai_taxonomies_a` e basta.
  `cluster_coverage_v` mostra i cluster che una fonte salterebbe in silenzio
  per mancanza di termine, e `verify_schema.py` lo segnala.
- **L'agenzia dichiarata dalla fonte vince sulla nostra lista.**
  `org_linkedin_recruitment_agency_derived` ha copertura **100%** e su un giro
  reale ha riconosciuto **95 agenzie che i nostri pattern avrebbero mancato** —
  «Newslot Recrutement», «Atomic HR», «GE IROISE»: piccole società di selezione
  locali, che una lista mantenuta a mano non potrà mai enumerare. Un
  `declared = false` però **non** annulla la lista: la fonte può non riconoscere
  un'agenzia che noi conosciamo, e le due evidenze si sommano.
- **In chiamata vanno SOLO paese e famiglia.** Tutti i filtri personali restano
  nel funnel. Un filtro spinto in chiamata restringe il corpus **condiviso**, e
  le offerte non scaricate ieri non tornano quando domani si iscrive qualcuno
  con preferenze più larghe. **Il risparmio di crediti non vale un archivio che
  dipende da chi era iscritto quel giorno.** Nel funnel un filtro costa zero e
  non lascia buchi.
- **Un cluster si chiede per TASSONOMIA, non per titolo.** `ai_taxonomies_a` non
  è solo un campo di risposta: è un **parametro di ricerca**, e cattura la
  famiglia professionale in qualunque lingua invece di costringerci a rincorrere
  i sinonimi. Misurato su 14 giorni:

  | Paese | `title='Human Resources'` | `ai_taxonomies_a='Human Resources'` |
  |---|---|---|
  | DE | 43 | 1.220 |
  | FR | 9 | 744 |
  | IT | 8 | 131 |

  I valori sono in `job_families`: 33 verificati. `ai_taxonomies_a_primary`
  esiste e restringe alla tassonomia principale, se servirà precisione.
- **L'arricchimento è opt-in solo su alcuni canali.** Su `active-ats` (e
  `modified-ats`) serve il flag; su **`active-jb`** — il canale board: LinkedIn,
  Wellfound, Y Combinator — i campi azienda ci sono **sempre**, perché
  l'organizzazione LinkedIn è letta direttamente dal board. *Verificato: una
  chiamata a `active-jb` senza flag riporta 17 campi `org_*`.* Serve il giorno
  in cui useremo quel canale — che oggi non usiamo, perché `link_kind` lo
  classificherebbe `job_board`.
- **`ats-organizations-advanced` non ci serve, e comunque non l'abbiamo.**
  Aggiunge Crunchbase, Glassdoor e serie storiche: dati da analisi di mercato,
  non da digest. Il Basic copre tutto ciò che usiamo. *Verificato: il nostro
  piano risponde `403`, come per `modified-ats`.* Esiste invece
  **`ats-organizations`**, che il piano accetta: è la strada se un giorno
  servirà una tabella `organizations` in cache.
- **Lo snapshot aziendale si aggiorna una volta al MESE**, il primo del mese
  alle 02:00 UTC. *Solo documentato, non verificabile in una sessione.* Conta
  per una eventuale cache: il ritmo di aggiornamento è mensile, non giornaliero,
  quindi rinfrescarla ogni notte spenderebbe chiamate per rileggere gli stessi
  dati.
- **L'arricchimento è OPT-IN.** Senza `include_basic_organization_details=true`
  la risposta ha 49 campi, con il flag 69 — fra cui `org_linkedin_size`,
  `org_linkedin_headcount`, `org_linkedin_industry` e
  `org_linkedin_recruitment_agency_derived`. Non costa crediti in più.
- **`time_frame` è obbligatorio sulla ricerca** e accetta solo `1h/24h/7d/6m`
  (non `1m`, che invece `count` tollera). Gli scaglioni sono grossolani, quindi
  si accompagna con `date_posted_gte`, che restringe dentro lo scaglione: `6m`
  dà 6.775 offerte, `6m` più la data ne dà 2.003. Siccome i crediti si pagano
  sulle offerte **restituite**, la data esatta è ciò che evita di pagare sei
  mesi per averne due settimane.
- **Fantastic dichiara la quota negli header**, e vanno creduti più delle nostre
  stime: `x-api-jobs-this-request` è il costo esatto della chiamata,
  `x-api-jobs-remaining` e `x-api-requests-remaining` sono il residuo reale. Il
  client usa il primo per la conciliazione — se un giorno la tariffa cambia,
  quell'header lo dice e un conteggio di record no. `x-api-jobs-remaining`
  sembra aggiornarsi con ritardo: va bene per un controllo di deriva
  periodico, non per decidere una singola chiamata.
- **Fantastic restituisce i paesi per NOME** ("Germany"), mentre France Travail
  dà "FR" e Arbetsförmedlingen "SE". Il client converte in ISO: mescolarli in
  `jobs.countries` romperebbe ogni filtro per paese in silenzio — la riga c'è,
  ma nessun `WHERE` la trova.
- **La fetch a pagamento non si chiama senza un sì esplicito, ogni volta.**
  I crediti sono una quota mensile condivisa con l'ingestione vera, che è il
  prodotto: spenderli per lavori di contorno la erode in silenzio, e chi paga
  non se ne accorge finché non mancano dove servono. Il permesso **non si
  deduce** dal fatto che una funzione è stata approvata — è l'errore fatto il
  2026-08-25, 400 crediti per costruire la striscia dei loghi del sito senza
  chiedere prima.

  `active-ats-count` e `/expired-ats` costano **zero crediti Jobs**: quelli
  restano liberi, e bastano a verificare quasi tutto. È `active-ats` che scala
  un credito per offerta restituita. Se serve un dato che costa, si dice prima
  quanto costa e si aspetta la risposta.

  *Nota per il sito: il filtro `organization` è a corrispondenza ESATTA sulla
  stringa dell'ATS — `Bosch` dà 0, `Bosch Group` dà 554. Una lista di aziende
  scritta a mano è muta senza darne segno.*
- **`count()` è OBBLIGATORIA prima di ogni scarico a pagamento.**
  `active-ats-count` costa 1 richiesta e zero crediti Jobs: su un piano a
  consumo, sapere quanto costerebbe una fetch prima di averla pagata vale più
  di una pagina di risultati. Un `fetch` su Fantastic senza `count` che lo
  precede è un errore, non una scorciatoia.
- **Il rate limit è una risorsa scarsa quanto i crediti.** Superarlo non costa
  denaro, costa un ban: da qui `clusters.daily_request_cap` e il token bucket per
  fonte. Ogni chiamata finisce in `api_usage`, anche quelle gratuite, perché una
  fonte che degrada va vista prima che diventi un problema.
- **Il fingerprint della deduplica morbida usa l'azienda, non il dominio.** Su un
  ATS condiviso (`aplitrak.com`, `varbi.com`, `greenhouse.io`, `lever.co`) il
  dominio identifica il fornitore del software, non chi assume: due aziende
  diverse collidevano e una veniva scartata come duplicato. Include anche la
  città, perché in una deduplica morbida un falso positivo fa sparire
  un'offerta vera mentre un falso negativo lascia solo una riga in più — meglio
  precisione che copertura.
- **Le agenzie si etichettano, non si filtrano.** `jobs.employer_kind` vale
  `direct` o `staffing_agency`, derivato da trigger a partire da
  `staffing_agency_patterns`. Diventerà un filtro utente nel funnel: c'è chi le
  agenzie le vuole e chi no, e sono due preferenze entrambe legittime.
  **Buttare dati in ingestione è irreversibile, etichettarli no.**

  La lista sta in tabella e non in codice per un motivo preciso: se fosse in
  codice, aggiungere un'agenzia domani non riclassificherebbe le offerte già
  ingerite. Il ciclo è `INSERT` + `SELECT reclassify_employers()`, senza
  rilascio. `verify_schema.py` segnala se le etichette si sono disallineate
  dalla lista.

  Il confronto è a **confine di parola**, non contenimento: "Randstadt Bakery"
  non è Randstad, e un'etichetta sbagliata su un datore vero è peggio di
  un'etichetta mancante. Per la stessa ragione i pattern hanno un minimo di 4
  caratteri, e parole comuni come "actual" non entrano da sole.

  **Tre valori, non due.** `undisclosed` quando la fonte non espone il nome del
  datore — il 5% delle offerte francesi. Due regole per il digest, da applicare
  quando ci arriveremo:

  **La preferenza è dell'utente, e va esposta.** `user_clusters.accepted_employer_kinds`
  esiste perché il motore la preveda; **l'onboarding del sito e il pannello
  preferenze li fa l'altro repo**. Requisito di prodotto: la scelta si fa in
  onboarding e resta modificabile dal pannello, non è una tantum. Il default
  accetta tutti e tre i tipi — restringere è una scelta esplicita dell'utente,
  mai un effetto collaterale.

  1. **Su `undisclosed` non si stampa mai un nome.** Si mostra l'etichetta
     «datore non dichiarato». Una stringa di ripiego spacciata per azienda è
     una bugia all'utente, ed è il motivo per cui `organization` ora può essere
     `NULL` invece di contenere un segnaposto.
  2. **A parità di punteggio l'ordine è `direct` → `staffing_agency` →
     `undisclosed`**, da `employer_kinds.rank`, come per `link_kinds`.

  **La lista è incompleta per costruzione.** Misurata su 796 datori reali
  copriva il 15,7%, salita al 25,4% dopo le aggiunte della 0015. Non arriverà
  mai al 100%: va rivista quando i dati mostrano nomi nuovi.
- **France Travail non dà mai link diretti.** Misurato su 1050 offerte in 7
  query: `origineOffre.origine` vale `1` nel 100% dei casi, cioè l'annuncio è
  ospitato da France Travail e non esiste URL di partner. Per la Francia la
  fonte dei `career_site` è quindi Fantastic: France Travail è un complemento
  che allarga la copertura, non la sostituisce. Ogni offerta francese porterà
  l'etichetta «via France Travail», e a parità di punteggio starà sotto un
  `career_site`.
- **`experienceLibelle` di France Travail vale più del codice.** `experienceExige`
  ha tre valori (`D`/`S`/`E`) che non bastano a scegliere uno scaglione, ma
  `experienceLibelle` porta gli anni reali ("2 An(s)", "7 An(s)"): da lì
  `ai_experience_level` si mappa sul 100% del campione, senza LLM. `S` ed `E` da
  soli restano `NULL`: dicono che serve esperienza, non quanta.
- **`trancheEffectifEtab` è la dimensione azienda**, che avevamo dato per non
  disponibile. C'è su France Travail. Non basta a riabilitare il filtro
  `user_clusters.company_sizes` — servirebbe su tutte le fonti — ma va ricordato
  quando ci torneremo.
- **Arbetsförmedlingen dà il segnale di rimozione nativo.** Lo JobStream espone
  `removed` e `removed_date`, quindi lì la scadenza non va dedotta dall'assenza.
  Il `link_kind` si ricava da `application_details`: se `via_af` è falso e c'è
  un URL, quello è l'ATS aziendale ed è `career_site`; altrimenti si ripiega
  sulla pagina di Platsbanken, che è `national_agency`.

### Logo dell'azienda

Catena di ripiego, in ordine. Fill-rate misurato su 300 offerte reali in
IT/DE/FR:

| | Copertura | |
|---|---|---|
| `org_logo_permalink` | **83%** | fonte principale |
| `organization_logo` | 49% | secondo anello (l'avevamo stimato al 40%) |
| Logo.dev da `domain_derived` | 98% ha il dominio | terzo anello |
| monogramma con le iniziali | sempre | ultimo, non fallisce mai |

I primi due insieme coprono il 94%. Col terzo si arriva praticamente al 100%,
e il quarto chiude il caso residuo senza mai lasciare un buco nel digest.

**Il logo si scarica e si salva UNA VOLTA PER AZIENDA. Mai collegato al volo.**
Nelle email i client bloccano le immagini remote per impostazione predefinita:
un logo collegato a un dominio esterno resta un rettangolo vuoto per la maggior
parte dei destinatari. Va scaricato una volta, salvato, e servito da noi —
il che significa anche che un cambio di logo lato azienda non si propaga da
solo, ed è accettabile.

Una volta per **azienda**, non per offerta: la stessa azienda pubblica decine di
annunci, e scaricare lo stesso file ogni volta sarebbe sprecato. La chiave
stabile è `org_linkedin_slug` (96%), con `domain_derived` (98%) come ripiego.

### Filtri promessi all'utente

In `user_filters`, con il fill-rate misurato accanto. Promessi: modalità di
lavoro, esperienza, tipo di contratto, lingua, sponsorship del visto, agenzie —
tutti con campo pieno al 100% — e **dimensione azienda**.

Non promessi: **titolo di studio** (48%) e **stipendio come filtro** (24%). Lo
stipendio si **mostra quando c'è**, non ci si filtra: filtrarci nasconderebbe
tre offerte su quattro per assenza di dato, non per scelta dell'utente.

Sulla dimensione azienda, due regole:

1. **Soglie numeriche, non fasce.** `min_headcount`/`max_headcount`, non
   `organization_size`. Il formato delle fasce differisce fra filtro
   (`"2-10"`) e campo (`"2-10 employees"`), e quell'ambiguità ci ha quasi fatto
   concludere che il dato non fosse utilizzabile. Un intero non ha formati da
   sbagliare.
2. **Le offerte senza il dato NON si escludono.** `org_headcount` arriva solo
   da Fantastic: escluderle significherebbe che chi cerca grandi aziende perde
   tutte le offerte francesi e svedesi soltanto perché passate da un'altra fonte.

### Sweep delle scadute

Tre segnali, in ordine di affidabilità:

| Segnale | Stato | Note |
|---|---|---|
| la fonte lo dichiara | `removed` | Fantastic `/expired-ats`, Arbetsförmedlingen JobStream. **Entrambi gratuiti** |
| `date_valid_through` passata | `expired` | Certo ma raro: il campo c'è sul 15% |
| non più vista | `expired` | Deduzione, l'unica che può sbagliare |

**Il terzo vale SOLO dopo una fetch completa.** Su una fetch troncata dal tetto o
dal limite di scorrimento della fonte, l'assenza di un'offerta non significa che
sia sparita: significa che non siamo arrivati a leggerla. Senza questa
condizione si ucciderebbero offerte vive, e sistematicamente quelle dei cluster
più grandi — che sono proprio quelli che si troncano.

`expiry_blind_spots_v` mostra quante offerte stanno in cluster che si troncano:
lì le morte restano attive finché la fonte non le dichiara. È un limite noto,
non un guasto.

`/expired-ats` di Fantastic ha un **terzo vocabolario** di `time_frame`
(`1h/1d/1m/6m`), diverso sia dalla ricerca (`1h/24h/7d/6m`) sia da `count`.
Restituisce una lista di soli id — 3,2 milioni per il mese — a costo zero.

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

- `user_clusters.company_sizes` esiste ma **il funnel non deve applicarlo**.
  **Non più per un limite tecnico, e il fill-rate regge**: `org_linkedin_size`
  è presente sul **97,5%** di un campione di 200 offerte in 4 paesi, e su un
  giro reale sul 96% delle offerte Fantastic. Fantastic espone `org_linkedin_size` e
  `org_linkedin_headcount` con `include_basic_organization_details=true`, ed
  `organization_size` è anche un parametro di ricerca. Ma le fonti nazionali non
  ce l'hanno, e un filtro attivo su una fonte sola darebbe risultati che
  dipendono da **dove è passata l'offerta** invece che da cosa cerca l'utente.
  Riaprirlo è ora una decisione di prodotto.
- `experience_levels` è **confermato**: un'offerta reale di Fantastic riporta
  `5-10`. La scala ipotizzata era giusta. `scripts/verify_schema.py` segnala i valori fuori vocabolario:
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

python -m nivult.matching.worker         # digest: valuta e consegna chi è dovuto
python -m nivult.matching.worker --user <uuid> --dry-run   # prova senza inviare

python scripts/reset_and_verify.py       # azzera, riapplica tutto, verifica. Da rilanciare a ogni modifica.
python scripts/verify_schema.py          # struttura, sola lettura, sicuro in produzione
python scripts/check_constraints.py      # i vincoli rifiutano davvero? (solo db _test/_dev)
python scripts/check_modules.py          # lo strato Python committa davvero? (solo db _test/_dev)
python scripts/check_api.py              # l'API HTTP autentica e risponde? (solo db _test/_dev)
python scripts/check_oauth.py            # OAuth: state, claim, collegamento (solo db _test/_dev)
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

### OAuth: una seconda porta, non un secondo sistema

`nivult.oauth`, authorization code con PKCE e `nonce`. Il giro finisce **dove
finisce il magic link**: un gettone monouso di `login_tokens`, riscattato dal
sito su `/verify`.

**Il motivo di quel giro apparentemente lungo è che il token di sessione non
deve mai entrare in una URL** — finirebbe nel referrer, nella cronologia e nei
log dei proxy. In URL viaggia solo un gettone che vale una volta sola e cinque
minuti. Effetto collaterale gradito: il sito non guadagna nessuna rotta nuova.

`login_tokens.origin` dice come è nato il gettone e viene copiato in
`sessions.origin` al consumo. **L'origine sta sul token, non la sceglie chi
consuma:** una sessione OAuth marcata `magic_link` mentirebbe esattamente dove
serve la verità, cioè indagando su un accesso sospetto.

**Il consumo di un gettone OAuth non verifica l'email.** Solo il magic link
prova il possesso dell'indirizzo; per OAuth la decisione è già stata presa a
monte, dove si sa di quale provider fidarsi.

**Di quale email fidarsi — la regola che evita un takeover.** Un'identità nota
entra sempre. Su un'identità nuova che porta l'indirizzo di un account già
esistente, si collega **solo se il provider ha dato una prova**; altrimenti si
rifiuta e si manda a passare dal magic link, che la prova sa darla.

| Provider | Email fidata? |
|---|---|
| Google, `email_verified: true` | sì |
| Google, `email_verified` falso o assente | no |
| Microsoft, tenant consumer (account personali) | sì |
| Microsoft, qualunque altro tenant | **no** |

L'ultima riga non è prudenza generica: sugli account aziendali il claim `email`
lo scrive l'amministratore del tenant, quindi **chiunque controlli un tenant
può presentarsi con l'indirizzo di qualcun altro**. È la vulnerabilità nota come
nOAuth, e la difesa raccomandata da Microsoft stessa è non usare mai l'email
come criterio di autorizzazione. `scripts/check_oauth.py` presidia questo caso.

**La firma dell'`id_token` non viene verificata, ed è una scelta.** Si
controllano `iss`, `aud`, `exp` e `nonce`; per l'integrità ci si fida del TLS,
perché il token arriva dal token endpoint in una chiamata server-to-server con
client confidenziale — il caso in cui l'OIDC consente esplicitamente di
saltarla. Se servirà, JWKS si innesta in `_verifica_claim` senza toccare altro.

**I redirect URI sono configurazione condivisa con le due console.**
`API_URL` costruisce `{API_URL}/auth/oauth/{provider}/callback`: cambiarlo qui
senza cambiarlo su Google e Azure rompe il login con un errore che non somiglia
alla sua causa.

**Privacy e termini vanno messi in ENTRAMBE le console, non solo in Google.**
Su Google stanno in *Google Auth Platform → Branding*; su Azure in
*App registrations → Branding & properties*. Dimenticare Azure non dà errore:
la schermata di consenso Microsoft mostra i segnaposto grezzi del suo modello
(`<appTerms>`, `<appPrivacy>`) e la frase «l'autore non ha fornito collegamenti
per la verifica delle condizioni» — che è esattamente ciò che non vuoi far
leggere a qualcuno mentre gli chiedi di fidarsi.

Il **publisher domain** di Azure si verifica servendo
`https://nivult.com/.well-known/microsoft-identity-association.json` con
dentro l'application id. Sta in `public/` nel repo del sito: è l'unica
directory che l'export statico copia alla lettera, e il punto iniziale di
`.well-known` sopravvive all'export — cosa da sapere prima che qualcuno lo
sposti «per ordine».

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
Riesce a controllare che il file sia una struttura CMS valida, che il suo
sha256 coincida con quello sulla Storage Box, e che non sia sospettosamente
piccolo — ma che il contenuto sia ripristinabile lo può dire solo un ripristino
di prova fatto fuori, con la chiave privata. Va fatto ogni trimestre.

**Eseguito il 2026-08-23** su un database di prova da 115 MB e 200.000 righe:
dump → cifratura → Storage Box → download → decifratura sul Mac → ripristino in
un container `pgvector/pgvector:pg17`. Impronta dei dati identica all'originale,
indici e ruoli ripristinati.

Se la copia off-site non è configurata o fallisce, lo script **esce in errore**.
Un backup che vive solo sul disco del database non è un backup, è una copia, e
un off-site che smette in silenzio è il modo classico di scoprirlo troppo tardi.

Ripristino:

```bash
openssl cms -decrypt -inform DER -in nivult-AAAA-MM-GG.sql.gz.enc \
  -inkey nivult-backup-PRIVATE.pem | gunzip | psql -U postgres
```

### Ruoli Postgres

- `nivult_migrator` — DDL. Lo usa **solo** il runner di migrazioni.
- `nivult_app` — solo `SELECT/INSERT/UPDATE/DELETE`. Lo usa tutto il resto.
  Niente `TRUNCATE`, niente `REFERENCES`, niente DDL.

I ruoli nascono dalla migrazione 0010 **senza password e senza LOGIN**: in un
file versionato non entrano segreti. Le credenziali le assegna
`deploy/setup-roles.sh` leggendo dall'ambiente.

`ALTER DEFAULT PRIVILEGES` non è un dettaglio: senza, la prima tabella creata da
una migrazione futura sarebbe invisibile a `nivult_app`, e l'applicazione si
romperebbe in produzione subito dopo un deploy riuscito, con un errore di
permessi che non assomiglia alla sua causa. `check_roles.py` lo verifica
creando una tabella dopo i GRANT e rileggendola come applicazione.

### Segreti nei commit

Hook `pre-commit` versionato in `.githooks/`, attivo con
`git config core.hooksPath .githooks` (una volta per clone). Usa `gitleaks` se
c'è, altrimenti una scansione a pattern di riserva.

Il controllo che conta davvero gira in **CI**, dove `gitleaks` c'è sempre e
guarda tutta la storia: un hook si salta con `--no-verify`, la pipeline no.

### Stato del deploy in produzione

Al 2026-08-23:

- 11 migrazioni applicate, schema verificato con `verify_schema.py` (sola
  lettura, l'unica suite che si può puntare alla produzione — le altre tre
  scrivono, e `reset_and_verify.py` comincia con `DROP SCHEMA public CASCADE`);
- password del superutente `nivult` ruotata, verificata da fuori: la nuova
  funziona, la vecchia è respinta;
- `nivult_app` e `nivult_migrator` attivi. L'applicazione usa `DATABASE_URL`
  (solo DML), il runner `MIGRATOR_DATABASE_URL`;
- backup riprovato subito dopo la rotazione, non aspettando le 03:00: 20 tabelle
  e 3 ruoli nel dump, cifratura e copia off-site verificate.

Il database di produzione ascolta solo su `127.0.0.1`: per lavorarci da locale
serve `ssh -L 15432:127.0.0.1:5432 root@<host>`.

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

- **Il ripristino va fatto sulla stessa immagine.** Il database di produzione
  nasce con `LOCALE = 'en_US.utf8'`, che su macOS non esiste: un
  `CREATE DATABASE` da quel dump fallisce con `invalid LC_COLLATE locale name`.
  Si ripristina dentro `pgvector/pgvector:pg17`, decifrando dove sta la chiave
  privata e mandando il dump in chiaro via SSH nel container. Scoperto facendo
  il ripristino, non leggendo lo script.
- **`gitleaks` non è installato sul Mac**, quindi l'hook usa la scansione di
  riserva. In CI gira quello vero.
- **Il workflow CI non è mai stato visto girare** da qui: `gh` non è installato.
- **`pg_hba` del container ha `trust` per 127.0.0.1 interno.** Significa che una
  verifica di password fatta con `docker exec ... psql -h 127.0.0.1` passa
  sempre, qualunque password si usi, e non dimostra niente. Le credenziali si
  verificano **da fuori**, attraverso la porta mappata sull'host.

## Metodo di lavoro

- **Prima di modifiche non banali, proponi il piano e aspetta conferma.**
- **Non over-ingegnerizzare:** la soluzione più semplice che funziona.
- **Ogni pezzo deve essere testabile da solo, da riga di comando.**
