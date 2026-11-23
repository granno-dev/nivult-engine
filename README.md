# nivult-engine

Backend di Nivult. Le regole del progetto stanno in [CLAUDE.md](CLAUDE.md).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
cp .env.example .env             # e compila DATABASE_URL
git config core.hooksPath .githooks   # hook anti-segreti, una volta per clone
```

Postgres sulla VPS ascolta solo su `127.0.0.1`. Per lavorarci da locale serve un
tunnel:

```bash
ssh -L 5432:127.0.0.1:5432 <utente>@<host>
```

## Migrazioni

```bash
python -m nivult.migrate status
python -m nivult.migrate up --dry-run
python -m nivult.migrate up
```

File SQL numerati in `migrations/`, applicati in ordine, uno per transazione,
sotto advisory lock. Una migrazione già applicata **non si modifica**: il runner
confronta i checksum e si ferma.

Una migrazione può rinunciare alla transazione con `-- nivult:no-transaction`
fra le prime righe (serve ad `ALTER DATABASE` e a `CREATE INDEX CONCURRENTLY`).
In quel caso deve essere idempotente.

## Verifica

```bash
python scripts/reset_and_verify.py    # azzera, riapplica tutto da zero, verifica
python scripts/verify_schema.py       # struttura. Sola lettura, sicuro in produzione.
python scripts/check_constraints.py   # vincoli SQL. Solo su database _test/_dev.
python scripts/check_modules.py       # strato Python. Solo su database _test/_dev.
python scripts/check_roles.py         # privilegi del ruolo app. Solo su _test/_dev.
python scripts/check_billing.py       # crediti, webhook ed entitlement; DB isolato.
python scripts/check_ingestion.py     # ingestione e migrazioni; DB isolato.
python scripts/check_cv.py            # CV, brand e consegna; senza DB o rete.
python scripts/check_canarino.py      # controlli e notifiche; senza DB o rete.
python scripts/check_backup_stream.py # decifratura dei backup CMS grandi, senza dati reali.
python scripts/check_prescore.py      # contratto 0–100 del pre-scorer; senza rete reale.
```

`check_canarino.py` copre notifiche disattivate, errori di invio e scheduler:
ogni quota CPU nel crontab deve avere `%` escaped (`400\%`), perché un `%`
nudo tronca il comando e può impedire gli export senza un errore evidente.

`check_backup_stream.py` crea CMS sintetici e verifica `decifra_backup.py`:
decifratura fedele a blocchi e rifiuto dei file troncati. Serve perché il solo
`openssl cms -decrypt` non decifra in streaming i backup grandi; hash e
dimensione non dimostrano che il contenuto sia recuperabile.

`verify_schema.py` controlla tabelle, indici, vincoli, funzioni, trigger,
`hnsw.iterative_scan`, la versione di pgvector, il numero di migrazioni presenti
nel repo e segnala la deriva del vocabolario del fornitore.

`check_modules.py` prova `nivult.retention` e `nivult.gdpr`: che committino
davvero — le asserzioni leggono da una **seconda connessione**, quindi passano
solo se il lavoro è committato — e che rifiutino esplicitamente una connessione
con una transazione già aperta invece di degradare a savepoint.

`check_constraints.py` prova che i vincoli **rifiutino davvero** i dati
incoerenti: canale di consegna senza recapito, digest doppio sulla stessa
finestra, la stessa offerta riproposta allo stesso utente, una voce di digest
che punta al match di un altro utente, sforamento del budget di cluster.
Gira tutto dentro una transazione annullata alla fine.

`reset_and_verify.py` è quello da rilanciare a ogni modifica dello schema: fa
`DROP SCHEMA public CASCADE`, azzera anche le impostazioni di database (così 0001
viene messa alla prova e non trova il lavoro già fatto), riapplica le 90
migrazioni 0001–0090 ed esegue `verify_schema`, `check_constraints`,
`check_modules`, `check_api`, `check_urls`, `check_roles`, `check_oauth`,
`check_billing`, `check_ingestion` e i gate match-v3 su ranking, cache oltre
1.500 offerte, metriche, spiegazioni e hard negatives. `check_cv`,
`check_canarino`, `check_backup_stream` e `check_prescore` restano suite separate
perché non richiedono un database.

Distruttivo: gira solo su database che finiscono per `_test`/`_dev`.

La CI esegue `reset_and_verify.py` e poi `prova_api_clienti.py`,
`prova_api_dati.py`, `prova_salari.py`, `check_cv.py`, `check_canarino.py`,
`check_backup_stream.py` e `check_prescore.py`: le suite billing e ingestion
sono quindi comprese nel reset, le altre sono esplicite nel passaggio successivo.

## Azioni personali, candidature e date

`GET /me/offerte` offre le viste `match`, `azioni`, `salvate`, `candidature`,
`scartate` e `archivio`, con paginazione `limite`/`offset`. Il filtro
`ricerca_id` usa l'UUID della ricerca in `user_clusters`; il vecchio UUID del
cluster è accettato solo se identifica una sola ricerca. Lo storico non dipende
dalla soglia o dall'eleggibilità corrente. `archiviata` continua a indicare la
lapide tecnica `jobs.purged_at`; `in_archivio` è invece l'azione dell'utente.
La vista `azioni` include anche il lavoro tracker anteriore alla candidatura:
note, contatto, prossima azione/data, colloqui e documenti presenti mantengono il match
nello storico; quando non resta alcun lavoro personale, il match ne esce.

`PUT /me/offerte/{match_id}/stato` aggiorna lo stato personale completo con
`base_version`: i replay identici sono idempotenti, i cambiamenti incrementano
`action_version` e un conflitto risponde 409 con l'`offerta` canonica. Gli stati
`applied`, `interview`, `offer` e `rejected` sono dichiarazioni manuali
dell'utente, non conferme ATS. Non esiste auto-apply né una verifica del
provider associata alla mutation; aprire un annuncio non equivale a candidarsi.
La stessa mutation accetta `note`, `contatto`, `prossima_azione` e `prossima_azione_il`; i campi
omessi restano invariati. Colloqui multipli e documenti si gestiscono sotto
`/me/offerte/{match_id}/colloqui` e `/documenti`: ogni mutation incrementa
`action_version`. I documenti PDF/DOCX/TXT (massimo 10 file e 50 MB per offerta,
10 MB per file) sono cifrati a busta come i CV; il download richiede la sessione e
usa `no-store`, `nosniff` e attachment. La cancellazione GDPR raccoglie anche le
loro chiavi storage.

`GET /me/riepilogo-settimanale` restituisce pipeline corrente, candidature della
settimana locale, prossimi colloqui/azioni/scadenze (massimo 10 offerte per lista),
motivi di scarto e digest effettivamente ricevuti. `valid_through` espone soltanto
`jobs.date_valid_through`, mai una scadenza dedotta da freschezza o ultima verifica.
Il worker `python -m nivult.tracker_notifications` invia promemoria Expo per
colloqui, azioni, scadenze/chiusure e riepilogo del lunedi', con deduplica dopo
l'invio e politica at-least-once; i wrapper sono `deploy/tracker-notifications.sh`
e `deploy/mavult-tracker-notifications.sh`.

`pubblicata_il` esce solo quando `jobs.date_posted_declared` è `TRUE`: stime e
fallback restano utili all'ingestione ma non diventano date di pubblicazione.
`prima_rilevazione` e `ultima_verifica` hanno significato distinto. I nuovi
digest escludono scartati, archiviati e candidature avviate; nuovo CV e
correzione profilo eliminano soltanto i match non consegnati e senza azioni.

## Frequenza del digest per ricerca

Dalla migrazione 0087 ogni riga `user_clusters` può avere `frequency`, giorno,
ora e fuso propri; `frequency = NULL` significa ereditare il default account.
`users.next_digest_at` resta la scadenza delle ricerche ereditate, mentre gli
override usano `user_clusters.next_digest_at`. `GET /me` espone il minimo fra
queste scadenze senza modificare quella globale.

Nei payload dei filtri, l'assenza di tutti i campi di pianificazione conserva
override e scadenza. Se almeno uno è presente, `frequency` è obbligatoria e la
nuova pianificazione deve essere coerente: daily senza giorni, weekly con solo
`send_weekday`, monthly con solo `send_monthday`; ora e fuso nulli o assenti
usano il fallback account. `frequency: null` rimuove l'override. I campi
`next_digest_at` e `last_digest_at` sono soltanto lettura e il client non può
scriverli. `POST /me/ricerca` restituisce le date della riga salvata;
`GET /me/cluster` espone la pianificazione dentro `filtri` e le due date a
livello della ricerca.

Uno slot combina soltanto le ricerche dovute e deduplica offerte e valutazioni.
`last_digest_at` viene aggiornato soltanto per gli UUID con almeno un elemento
realmente incluso; `GET /me.ultimo_digest` è il massimo fra le due categorie.
Il cambio di ora o fuso account ricalcola soltanto gli override che ereditano
quel campo, lasciando invariati i valori espliciti. Prima del primo contenuto,
uno `skipped_empty` riprova il giorno dopo anche con frequenza mensile.

Chiusura e rischedulazione condividono un solo commit. Un pending o failed con
`digest_items` viene chiuso come consegna già avvenuta, senza rispedire né
ricreare item o rank; gli extra restano per lo slot successivo. `next_digest_at`
si aggiorna soltanto se scadenza e configurazione iniziali sono ancora correnti,
così un retry non sovrascrive una pianificazione cambiata nel frattempo e non
sposta due volte uno slot già avanzato. Il dry-run annulla anche il recupero.

## Ingestione

```bash
python -m nivult.ingestion.probe france_travail --query "ressources humaines" --country FR --limit 5
python -m nivult.ingestion.runner --all --dry-run
python -m nivult.ingestion.runner --all --dry-run --allow-paid
python -m nivult.ingestion.runner --all
python -m nivult.ingestion.sweep          # scadute, in cron dopo il runner
```

Fantastic e' pensionato: runner, sweep e probe non lo chiamano piu'. Le offerte
storiche restano; non vengono piu' fatte chiamate a `/expired-ats`.

Il dry-run scarica e normalizza ma non persiste offerte, run, budget, usage o
cursori. Senza `--allow-paid` salta le fonti a pagamento; con il flag le chiama
davvero e il costo del fornitore resta reale, pur senza addebitare i budget
locali.

## Retention offerte

```bash
python scripts/purge_jobs.py --dry-run
python scripts/purge_jobs.py            # in cron, una volta al giorno
python scripts/purge_jobs.py --stats
```

Elimina le offerte `expired`/`removed` da più di 60 giorni. Quelle referenziate
da un match o da un digest restano come lapide senza jsonb né embedding, per non
perdere l'anti-ripetizione. Gli aggregati per cluster e mese vengono scritti
prima della cancellazione.

## Server e backup

Configurazione versionata in `deploy/`. Il backup notturno nasce dalla replica
sull'N5 (`deploy/backup-n5.sh`), cifra con chiave pubblica e copia su Hetzner
Storage Box; `deploy/backup.sh` resta il ripiego sul server. La chiave privata
non sta sul server. Per il ripristino dei file grandi si usa
`scripts/decifra_backup.py`, verificato da `scripts/check_backup_stream.py`.
Vedi la sezione Sicurezza di [CLAUDE.md](CLAUDE.md).

## Cancellazione utente (GDPR)

```bash
python scripts/delete_user.py --user-id <uuid>
python scripts/delete_user.py --list-pending
```

A lotti, in transazioni brevi. La richiesta resta aperta finché i file su object
storage non sono stati rimossi, non solo le righe.
