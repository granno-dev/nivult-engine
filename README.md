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
```

`verify_schema.py` controlla tabelle, indici, vincoli, funzioni, trigger,
`hnsw.iterative_scan`, la versione di pgvector, e segnala la deriva del
vocabolario del fornitore.

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
viene messa alla prova e non trova il lavoro già fatto), riapplica le sette
migrazioni e passa le due verifiche. Sul database di sviluppo ci mette ~1,5s.

Distruttivo: gira solo su database che finiscono per `_test`/`_dev`.

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

Configurazione versionata in `deploy/`. Il backup gira alle 03:00, cifra con
chiave pubblica e copia su Hetzner Storage Box; la chiave privata non sta sul
server. Vedi la sezione Sicurezza di [CLAUDE.md](CLAUDE.md).

## Cancellazione utente (GDPR)

```bash
python scripts/delete_user.py --user-id <uuid>
python scripts/delete_user.py --list-pending
```

A lotti, in transazioni brevi. La richiesta resta aperta finché i file su object
storage non sono stati rimossi, non solo le righe.
