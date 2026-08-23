# deploy/

Configurazione del server, versionata. Nessun segreto qui dentro.

| File | Destinazione | Note |
|---|---|---|
| `backup.sh` | `/opt/nivult/backup.sh` (0700) | cron 03:00 |
| certificato pubblico | `/opt/nivult/backup-recipient.pem` (0644) | **non versionato**: `.gitignore` blocca ogni `*.pem`, e la regola resta assoluta per non lasciare varchi |
| `backup.env.example` | `/opt/nivult/backup.env` (0600) | credenziali Storage Box, **mai** nel repo |
| `52nivult-security` | `/etc/apt/apt.conf.d/` | solo canale security, riavvio 04:00 |

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

```bash
openssl cms -decrypt -inform DER -in nivult-AAAA-MM-GG.sql.gz.enc \
  -inkey nivult-backup-PRIVATE.pem | gunzip | psql -U postgres
```
