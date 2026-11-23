# deploy/

Configurazione del server, versionata. Nessun segreto qui dentro.

| File | Destinazione | Note |
|---|---|---|
| `backup.sh` | `/opt/nivult/backup.sh` (0700) | ripiego sul server |
| `backup-n5.sh` | `/root/backup-n5.sh` sull'N5 | backup notturno dalla replica, senza lock sul primario |
| certificato pubblico | `/opt/nivult/backup-recipient.pem` (0644) | **non versionato**: `.gitignore` blocca ogni `*.pem`, e la regola resta assoluta per non lasciare varchi |
| `backup.env.example` | `/opt/nivult/backup.env` (0600) | credenziali Storage Box, **mai** nel repo |
| `52nivult-security` | `/etc/apt/apt.conf.d/` | solo canale security, riavvio 04:00 |
| `cron.sh` | resta in `deploy/` | installa TUTTI i lavori periodici, senza cancellare gli altri |

## I lavori periodici si installano con `cron.sh`, mai a mano

```bash
sudo deploy/cron.sh          # installa o aggiorna
sudo deploy/cron.sh --check  # verifica e basta, esce 1 se manca qualcosa
```

| Orario | Lavoro | Perche' li' |
|---|---|---|
| 01:00 | ingestione (`nightly.sh`) | prima dei digest: il primo giro del mattino deve trovare le offerte della notte |
| 02:30 | ATS/Wikidata (`ats-nightly.sh`) | dopo l'ingestione, prima del backup |
| 03:13 | backup (`backup-n5.sh` sull'N5) | dalla replica, senza lock sul primario; `backup.sh` resta il ripiego |
| ogni ora al :10 | digest (`digests.sh`) | l'orario di invio e' quello dell'utente, nel suo fuso: un giro al giorno consegnerebbe in ritardo |
| ogni 15 minuti | digest Mavult (`mavult-digests.sh`) | wrapper dello stesso runner con base, lock e ambiente `/opt/mavult` |
| ogni 5 minuti | promemoria tracker (`tracker-notifications.sh`) | colloqui, azioni, scadenze/chiusure e riepilogo del lunedi'; deduplica dopo l'invio, quindi retry at-least-once |
| ogni 5 minuti | promemoria Mavult (`mavult-tracker-notifications.sh`) | wrapper con base, lock e ambiente `/opt/mavult`; installarlo nel crontab Mavult |
| 05:50 | pagine SEO (`nivult.ats.pagine_seo`) | dopo l'ATS delle 02:30: rigenera `vetrina/ats/`, `vetrina/job-board/`, le due sitemap e l'indice coi numeri vivi; log in `/var/log/nivult-pagine-seo.log` |

Il riepilogo push parte il lunedi' dalle 09:00 locali dell'utente. I promemoria di
colloquio rispettano `promemoria_minuti`; quelli di scadenza partono entro 72 ore.
Una riga `personal_notification_dedup` viene scritta solo dopo almeno un invio
Expo riuscito: un crash puo' ripetere un avviso, non perdere un evento come gia'
consegnato.

Con `PRESCORE_V3_ANN=1` il runner aggiorna gli embedding prima dei digest e usa
l'ANN solo con almeno il 98% di hash aggiornati. `PRESCORE_V3_ANN=0` disattiva il
percorso senza migrazioni inverse: la cache resta disponibile per un giro successivo.

Il controllo `canarino.py` sui digest dovuti considera sia
`users.next_digest_at` (ricerche ereditate) sia le scadenze degli override in
`user_clusters.next_digest_at`: un utente dovuto soltanto per una ricerca
personalizzata non resta invisibile agli allarmi. Nei log, un `401` del provider
e' un allarme immediato anche sotto la soglia degli altri errori; timestamp e
dettagli JSON non duplicano il conteggio e un ID numerico contenente `401` non
viene scambiato per un errore HTTP.

## Indice testi ATS

`ats_jobs_search_idx` usa `fastupdate = false`: la ricerca ha tempi di risposta
stabili e le scritture non alimentano una pending list che possa bloccare il
consolidamento in foreground. `src/nivult/ats/schema.sql` crea l'indice con
questo parametro e `runner.setup` lo applica agli ambienti legacy senza ripetere
il DDL quando e' gia' attivo. Dopo la conversione, una coda preesistente va
consolidata con `gin_clean_pending_list`; il parametro non la svuota da solo.
Non riattivare `fastupdate` senza misurare prima l'effetto sulle ricerche.

**Perche' esiste questo script.** Il 2026-08-28 alle 17:53 il crontab e'
stato riscritto a mano per installare `ats-nightly`, e `crontab <file>`
**sostituisce l'intera tabella**: sono spariti in un colpo i digest orari,
l'ingestione notturna e il backup. E' sopravvissuto solo il lavoro che si
stava installando.

Nessuno se n'e' accorto per quindici ore, perche' **un cron che non c'e'
piu' non fallisce: tace**. Il mattino dopo l'utente non ha ricevuto il
digest, la notte non era entrata nessuna offerta (quindi il digest sarebbe
comunque uscito vuoto), e il backup mancava da ventiquattro ore.

`cron.sh` e' idempotente e conservativo: rimpiazza solo le righe che
puntano a `/opt/nivult` e lascia intatto il resto della tabella. Chiamare
`--check` dopo ogni deploy costa un secondo e chiude questa classe di
guasti.

## Chiave dei backup

La coppia è stata generata sul Mac. Sul server è stato copiato **solo** il
certificato pubblico: quella macchina può produrre backup ma non rileggerli.

```
SHA256 6F:82:68:CD:F9:FC:76:4A:36:BE:28:19:8C:04:A3:98:A0:C8:CE:46:28:F1:43:5B:45:F6:3D:38:97:DC:F8:65
```

La privata sta in `~/nivult-backup-key/nivult-backup-PRIVATE.pem` e va copiata
nel password manager. **Senza, i backup sono irrecuperabili.**

Il certificato non è nel repo: nessun `*.pem` lo è. Non serve conservarlo a
parte, si rigenera dalla privata:

```bash
openssl req -x509 -new -key nivult-backup-PRIVATE.pem -days 7300 \
  -subj "/CN=nivult-backup/O=Nivult" -out nivult-backup-public.pem
```

## Storage Box

Chiave SSH dedicata sul server: `/root/.ssh/id_ed25519_storagebox`.
La pubblica va installata sulla Storage Box, poi si compila `/opt/nivult/backup.env`.

## Verifica

```bash
ssh root@<host> /opt/nivult/backup.sh      # deve uscire 0 e loggare "copia off-site verificata"
cat /opt/nivult/backup-state               # ok o failed, con data
```

Ripristino di prova, trimestrale, su una macchina con la chiave privata:

Dalla radice del repository, su una macchina con la chiave privata e un database
di prova Postgres 17 (mai direttamente sulla produzione):

```bash
python scripts/decifra_backup.py --key nivult-backup-PRIVATE.pem \
  < nivult-AAAA-MM-GG.sql.gz.enc | gunzip | psql -U postgres
```

`openssl cms -decrypt` non basta per questi file: la decifratura CMS di OpenSSL
carica il contenuto e fallisce sui backup grandi. Il lettore `decifra_backup.py`
decodifica i blocchi DER con memoria limitata; `scripts/check_backup_stream.py`
ne verifica il formato e il rifiuto dei file troncati. La verifica off-site di
hash e dimensione resta necessaria, ma non sostituisce questa prova.

## Credenziali France Travail

API pubblica ufficiale, gratuita. Registrazione:

1. Account su **https://francetravail.io** (sezione *Se connecter* → *Créer un compte*).
2. *Mon espace* → **Créer une application**. Servono nome, descrizione dell'uso
   e URL di callback (per `client_credentials` non viene usato: va bene
   `https://nivult.com`).
3. Nella scheda dell'applicazione, **souscrire** all'API
   **« Offres d'emploi v2 »**. La sottoscrizione può richiedere una validazione.
4. A sottoscrizione attiva, nella scheda dell'applicazione compaiono
   **Identifiant client** (comincia per `PAR_`) e **Clé secrète**.

Poi in `/opt/nivult/.env`:

```
FRANCE_TRAVAIL_CLIENT_ID=PAR_...
FRANCE_TRAVAIL_CLIENT_SECRET=...
```

Dettagli tecnici usati dal client:

| | |
|---|---|
| Token endpoint | `https://entreprise.francetravail.fr/connexion/oauth2/access_token?realm=/partenaire` |
| Grant | `client_credentials` |
| Scope | `api_offresdemploiv2 o2dsoffre` |
| Base API | `https://api.francetravail.io/partenaire/offresdemploi/v2` |
| Ricerca | `GET /offres/search` — risponde **206** con `Content-Range`, non 200 |
| Limiti | 150 risultati per pagina, 3149 per ricerca |

Verifica appena arrivano le credenziali:

```bash
python -m nivult.ingestion.probe france_travail --query "ressources humaines" --limit 5
```

Sola lettura, non tocca il database.
