#!/usr/bin/env bash
# Seconda copia dei backup cifrati sul N5 (regola 3-2-1: server + Storage
# Box sono entrambi Hetzner; questa e' la copia in un altro posto).
# Gira sul N5 (Unraid) alle 06:00 da /boot/config/plugins/dynamix/nivult-backup.cron.
#
# Tira i file da /opt/nivult/backups del server via Tailscale, con una
# chiave SSH che sul server puo' fare SOLO rsync in lettura di quella
# cartella (rrsync -ro). I file sono cifrati con la chiave pubblica: il
# N5 li conserva ma non puo' leggerli, come la Storage Box.
set -uo pipefail
DEST=/mnt/user/backup-nivult
LOG=/mnt/user/backup-nivult/copia.log
mkdir -p "$DEST"
{
  echo "$(date -Is) inizio"
  rsync -a --partial --timeout=600 \
    -e "ssh -i /root/.ssh/id_ed25519_nivult -o BatchMode=yes -o StrictHostKeyChecking=accept-new" \
    root@100.117.204.17:/ "$DEST/" \
    && echo "$(date -Is) ok: $(ls "$DEST"/nivult-*.enc 2>/dev/null | wc -l) file, $(du -sh "$DEST" | cut -f1)" \
    || echo "$(date -Is) ERRORE rsync (codice $?)"
  # 90 giorni come sulla Storage Box
  find "$DEST" -name 'nivult-*.sql.gz.enc' -mtime +90 -delete
} >> "$LOG" 2>&1
tail -1 "$LOG"
