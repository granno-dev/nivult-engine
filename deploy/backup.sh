#!/usr/bin/env bash
#
# Backup cifrato di Nivult, con copia off-site su Hetzner Storage Box.
#
# CIFRATURA A CHIAVE PUBBLICA. Sul server sta solo il certificato: questa
# macchina può produrre backup ma NON può rileggerli. La chiave privata vive
# fuori — password manager — e serve solo al ripristino. Se il server viene
# compromesso, l'attaccante non ottiene lo storico dei backup.
#
# Ripristino (su una macchina che ha la chiave privata):
#   openssl cms -decrypt -inform DER -in nivult-AAAA-MM-GG.sql.gz.enc \
#     -inkey nivult-backup-PRIVATE.pem | gunzip | psql -U postgres
#
# Configurazione in /opt/nivult/backup.env (0600, fuori dal repo).

set -euo pipefail

BASE=/opt/nivult
CONF="$BASE/backup.env"
RECIPIENT="$BASE/backup-recipient.pem"
LOCAL_DIR="$BASE/backups"
STATE="$BASE/backup-state"
CONTAINER=nivult-db-1
DB_SUPERUSER=nivult
LOCAL_KEEP_DAYS=14
REMOTE_KEEP_DAYS=90
MIN_BYTES=500

log()  { echo "$(date -Is) $*"; logger -t nivult-backup -- "$*" || true; }
die()  { log "ERRORE: $*"; printf 'failed\t%s\t%s\n' "$(date -Is)" "$*" > "$STATE"; exit 1; }

[ -r "$RECIPIENT" ] || die "certificato di cifratura assente: $RECIPIENT"
mkdir -p "$LOCAL_DIR"

STAMP=$(date +%F)
OUT="$LOCAL_DIR/nivult-$STAMP.sql.gz.enc"

# pg_dumpall e non pg_dump: include anche i ruoli, che non stanno dentro il
# database. Senza, un ripristino su una macchina nuova riparte senza utenti.
#
# set -o pipefail è essenziale qui: la versione precedente di questo script non
# ce l'aveva, quindi se pg_dumpall falliva gzip scriveva un file vuoto e il
# backup "riusciva" in silenzio.
log "dump in corso -> $OUT"
docker exec "$CONTAINER" pg_dumpall -U "$DB_SUPERUSER" \
  | gzip -9 \
  | openssl cms -encrypt -aes-256-cbc -binary -stream -outform DER \
      -recip "$RECIPIENT" -out "$OUT.part" \
  || die "dump o cifratura falliti"

mv "$OUT.part" "$OUT"
chmod 600 "$OUT"

# Verifiche possibili SENZA la chiave privata: che il file sia una struttura
# CMS valida e non sia sospettosamente piccolo. Che il contenuto in chiaro sia
# ripristinabile lo può dire solo un ripristino di prova fuori da qui.
SIZE=$(stat -c%s "$OUT")
[ "$SIZE" -ge "$MIN_BYTES" ] || die "backup troppo piccolo ($SIZE byte): dump vuoto?"
openssl cms -cmsout -noout -inform DER -in "$OUT" 2>/dev/null \
  || die "il file cifrato non è una struttura CMS valida"
log "backup locale ok: $SIZE byte"

# --- copia off-site ---------------------------------------------------------
# Un backup che vive solo sul disco del database non è un backup, è una copia.
# Se la copia remota non è configurata o fallisce, lo script ESCE IN ERRORE:
# meglio un cron che protesta ogni notte che un off-site dimenticato.
if [ ! -r "$CONF" ]; then
  die "copia off-site non configurata: manca $CONF (vedi deploy/README.md)"
fi
# shellcheck source=/dev/null
. "$CONF"
: "${BACKUP_REMOTE_HOST:?BACKUP_REMOTE_HOST non impostato in $CONF}"
: "${BACKUP_REMOTE_USER:?BACKUP_REMOTE_USER non impostato in $CONF}"
: "${BACKUP_REMOTE_PATH:=nivult}"
: "${BACKUP_REMOTE_PORT:=23}"
: "${BACKUP_SSH_KEY:=/root/.ssh/id_ed25519_storagebox}"

# -n è obbligatorio: senza, ssh legge da stdin e nel ciclo di potatura più
# sotto si mangerebbe la lista di file che gli viene passata via pipe.
SSH_OPTS="-n -p $BACKUP_REMOTE_PORT -i $BACKUP_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
RSYNC_SSH="ssh -p $BACKUP_REMOTE_PORT -i $BACKUP_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
SB="$BACKUP_REMOTE_USER@$BACKUP_REMOTE_HOST"
BASENAME=$(basename "$OUT")

log "invio a $SB:$BACKUP_REMOTE_PATH"
rsync -a --partial -e "$RSYNC_SSH" "$OUT" "$SB:$BACKUP_REMOTE_PATH/" \
  || die "copia off-site fallita"

# Confronto degli sha256, non delle dimensioni: la Storage Box espone
# sha256sum nella sua shell ristretta, quindi si può verificare che i byte
# arrivati siano gli stessi partiti invece di fidarsi del codice di uscita di
# rsync. Due file troncati alla stessa lunghezza avrebbero la stessa dimensione.
LOCAL_HASH=$(sha256sum "$OUT" | awk '{print $1}')
REMOTE_HASH=$(ssh $SSH_OPTS "$SB" "sha256sum $BACKUP_REMOTE_PATH/$BASENAME" 2>/dev/null | awk '{print $1}')
[ -n "$REMOTE_HASH" ] || die "il file non risulta sulla Storage Box"
[ "$LOCAL_HASH" = "$REMOTE_HASH" ] \
  || die "sha256 divergente: locale $LOCAL_HASH, remoto $REMOTE_HASH"
log "copia off-site verificata, sha256 $LOCAL_HASH"

# --- potatura ---------------------------------------------------------------
find "$LOCAL_DIR" -name 'nivult-*.sql.gz.enc' -mtime "+$LOCAL_KEEP_DAYS" -delete
find "$LOCAL_DIR" -name '*.part' -mtime +1 -delete

CUTOFF=$(date -d "-$REMOTE_KEEP_DAYS days" +%F)
ssh $SSH_OPTS "$SB" "ls $BACKUP_REMOTE_PATH" 2>/dev/null \
  | grep -oE 'nivult-[0-9]{4}-[0-9]{2}-[0-9]{2}\.sql\.gz\.enc' \
  | while read -r f; do
      d=${f:7:10}
      if [[ "$d" < "$CUTOFF" ]]; then
        log "potatura remota: $f"
        ssh $SSH_OPTS "$SB" "rm $BACKUP_REMOTE_PATH/$f" || log "impossibile rimuovere $f"
      fi
    done || true

printf 'ok\t%s\t%s byte\n' "$(date -Is)" "$SIZE" > "$STATE"
log "backup completato"
