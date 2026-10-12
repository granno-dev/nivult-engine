#!/usr/bin/env bash
#
# Backup cifrato di Nivult, DALLA REPLICA sull'N5 (12/10/2026).
#
# Nasce dal lock storm del 12/10: pg_dumpall su Hetzner leggeva 47 GB
# tenendo AccessShareLock per 6+ ore sul mattino pieno, e dietro di lui
# facevano coda TRUNCATE, benchmark e il ricalcolo del cruscotto. Il dump
# ora legge la replica streaming (nivult-replica, container sul N5):
# byte identici, zero lock in produzione.
#
# Tutto il resto e' identico a deploy/backup.sh: stessa cifratura a chiave
# pubblica (sull'N5 sta solo il certificato), stessa verifica sha256 con la
# Storage Box, stesso file di stato — scritto sul SERVER via ssh, perche'
# e' li' che la sentinella lo legge.
#
# Cron sull'N5 (host): 0 3 * * * /root/backup-n5.sh >> /var/log/nivult-backup.log 2>&1

set -euo pipefail

HETZNER=root@37.27.36.85
BASE=/mnt/cache/appdata/nivult-backup
RECIPIENT="$BASE/backup-recipient.pem"
LOCAL_DIR="$BASE/dumps"
CONTAINER=nivult-replica
DB_SUPERUSER=nivult
MIN_BYTES=500
STATE_RIGA=""

log() { echo "$(date -Is) $*"; }
die() {
  log "ERRORE: $*"
  printf 'failed\t%s\t%s\n' "$(date -Is)" "$*" | ssh -o BatchMode=yes -i /root/.ssh/id_ed25519_n5tohetzner "$HETZNER" \
    "cat > /opt/nivult/backup-state" || true
  exit 1
}

exec 9>/var/run/nivult-backup-n5.lock
if ! flock -n 9; then
  log "un altro backup e' in corso: salto"
  exit 0
fi

[ -r "$RECIPIENT" ] || die "certificato di cifratura assente: $RECIPIENT"
mkdir -p "$LOCAL_DIR"

# la replica deve essere viva e fresca: un backup da una replica morta e'
# un backup dei guai di ieri
LAG=$(docker exec "$CONTAINER" psql -U "$DB_SUPERUSER" -d nivult_ats -tAc \
  "SELECT COALESCE(extract(epoch FROM now() - pg_last_xact_replay_timestamp()), -1)::int") \
  || die "la replica non risponde"
if [ "$LAG" -lt 0 ] || [ "$LAG" -gt 900 ]; then
  die "replica non fresca (lag ${LAG}s): backup rifiutato, riparare la replica prima"
fi
log "replica fresca (lag ${LAG}s)"

STAMP=$(date +%F)
OUT="$LOCAL_DIR/nivult-$STAMP.sql.gz.enc"

log "dump in corso dalla replica -> $OUT"
docker exec "$CONTAINER" pg_dumpall -U "$DB_SUPERUSER" \
  | gzip -9 \
  | openssl cms -encrypt -aes-256-cbc -binary -stream -outform DER \
      -recip "$RECIPIENT" -out "$OUT.part" \
  || die "dump o cifratura falliti"
mv "$OUT.part" "$OUT"
chmod 600 "$OUT"

SIZE=$(stat -c%s "$OUT")
[ "$SIZE" -ge "$MIN_BYTES" ] || die "backup troppo piccolo ($SIZE byte): dump vuoto?"
HEAD_HEX=$(head -c 16 "$OUT" | od -An -tx1 | tr -d ' \n')
case "$HEAD_HEX" in
  30*06092a864886f70d010703*) ;;
  *) die "il file cifrato non comincia come una busta CMS EnvelopedData";;
esac
log "backup locale ok: $SIZE byte"

# --- copia off-site, identica a backup.sh -----------------------------------
CONF="$BASE/backup.env"
[ -r "$CONF" ] || die "copia off-site non configurata: manca $CONF"
# shellcheck source=/dev/null
. "$CONF"
: "${BACKUP_REMOTE_HOST:?manca in $CONF}"
: "${BACKUP_REMOTE_USER:?manca in $CONF}"
: "${BACKUP_REMOTE_PATH:=nivult}"
: "${BACKUP_REMOTE_PORT:=23}"
: "${BACKUP_SSH_KEY:=/root/.ssh/id_ed25519_storagebox}"
SSH_OPTS="-n -p $BACKUP_REMOTE_PORT -i $BACKUP_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
RSYNC_SSH="ssh -p $BACKUP_REMOTE_PORT -i $BACKUP_SSH_KEY -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
SB="$BACKUP_REMOTE_USER@$BACKUP_REMOTE_HOST"
BASENAME=$(basename "$OUT")

log "invio a $SB:$BACKUP_REMOTE_PATH"
rsync -a --partial -e "$RSYNC_SSH" "$OUT" "$SB:$BACKUP_REMOTE_PATH/" \
  || die "copia off-site fallita"

LOCAL_HASH=$(sha256sum "$OUT" | awk '{print $1}')
REMOTE_HASH=$(ssh $SSH_OPTS "$SB" "sha256sum $BACKUP_REMOTE_PATH/$BASENAME" 2>/dev/null | awk '{print $1}')
[ -n "$REMOTE_HASH" ] || die "il file non risulta sulla Storage Box"
[ "$LOCAL_HASH" = "$REMOTE_HASH" ] \
  || die "sha256 divergente: locale $LOCAL_HASH, remoto $REMOTE_HASH"
log "copia off-site verificata, sha256 $LOCAL_HASH"

# potatura locale: 7 giorni (il disco dell'N5 e' largo) e 90 giorni remota
find "$LOCAL_DIR" -name 'nivult-*.sql.gz.enc' -mtime +7 -delete
find "$LOCAL_DIR" -name '*.part' -mtime +1 -delete
CUTOFF=$(date -d "-90 days" +%F)
ssh $SSH_OPTS "$SB" "ls $BACKUP_REMOTE_PATH" 2>/dev/null \
  | grep -oE 'nivult-[0-9]{4}-[0-9]{2}-[0-9]{2}\.sql\.gz\.enc' \
  | while read -r f; do
      d=${f:7:10}
      if [[ "$d" < "$CUTOFF" ]]; then
        log "potatura remota: $f"
        ssh $SSH_OPTS "$SB" "rm $BACKUP_REMOTE_PATH/$f" || log "impossibile rimuovere $f"
      fi
    done || true

# lo stato vive sul server: la sentinella legge li'
printf 'ok\t%s\t%s byte\n' "$(date -Is)" "$SIZE" | ssh -o BatchMode=yes -i /root/.ssh/id_ed25519_n5tohetzner "$HETZNER" \
  "cat > /opt/nivult/backup-state" || die "stato non scritto sul server"
log "backup completato"
