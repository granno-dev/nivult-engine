#!/bin/bash
# Sposta sull'array del N5 tutto cio' che su Hetzner e' FREDDO, e lo fa ogni notte.
#
# Perche': Hetzner ha 75 GB, di cui 22 sono il database VIVO (che deve restare su disco
# locale veloce). Ogni notte arrivano ~9 GB fra backup ed export, e il 17/09/2026 il disco
# si e' riempito al 100% fermando il recupero testi. Il N5 ha 6 TB liberi sull'array.
#
# Cosa si sposta e cosa NO:
#   - export piu' vecchi di GIORNI_EXPORT           -> si'
#   - backup oltre i GIORNI_BACKUP giorni, SOLO se gia' presenti e identici sullo
#     spazio remoto (doppia copia verificata prima di cancellare)  -> si'
#   - modelli e dataset in gpu/ piu' vecchi di 2 giorni           -> si'
#   - pgdata, engine, .env                                        -> MAI
#
# Ogni file si CANCELLA solo dopo aver verificato che la copia remota ha la stessa
# dimensione. Se il N5 non risponde, lo script esce senza toccare nulla.
set -uo pipefail
# 22/09/2026: su Hetzner (75 GB, 4 liberi) restano SOLO il backup e l'export
# di oggi; tutto cio' che ha piu' di 24 ore va sul N5 (6 TB liberi). Il
# cron gira alle 06:40, DOPO backup (03:00) ed export (05:45): cosi' il
# file di ieri ha passato le 24 ore e quello di oggi resta.
GIORNI_EXPORT=${GIORNI_EXPORT:-0}
GIORNI_BACKUP=${GIORNI_BACKUP:-0}
GIORNI_GPU=${GIORNI_GPU:-1}
N5=root@100.119.200.7; K=/root/.ssh/id_ed25519_n5
SSH="ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=30 -i $K"
SCP="scp -q -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=30 -i $K"
BASE=/mnt/user/nivult-archivio
LOG=/var/log/nivult-archivio.log
dire() { echo "$(date -Is) $*" >> $LOG; }

$SSH $N5 "mkdir -p $BASE/exports $BASE/backups $BASE/gpu" 2>/dev/null || {
  dire "N5 non raggiungibile: non tocco niente"; exit 0; }
prima=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)

sposta() {   # $1 file, $2 sottocartella
  local f="$1" sub="$2" nome dim remota
  nome=$(basename "$f"); dim=$(stat -c %s "$f" 2>/dev/null) || return 1
  $SCP "$f" "$N5:$BASE/$sub/$nome" || { dire "copia fallita: $nome"; return 1; }
  remota=$($SSH $N5 "stat -c %s $BASE/$sub/$nome 2>/dev/null || echo 0")
  if [ "$remota" = "$dim" ]; then rm -f "$f"; dire "spostato $sub/$nome ($((dim/1048576)) MB)"; return 0; fi
  dire "dimensione diversa per $nome ($dim vs $remota): NON cancello"; return 1
}

# 1. export freddi
while IFS= read -r f; do sposta "$f" exports; done \
  < <(find /opt/nivult/exports -maxdepth 1 -name "*.jsonl.gz" -mtime +$GIORNI_EXPORT -type f | sort)

# 2. backup: solo quelli gia' verificati sullo spazio remoto
set -a; . /opt/nivult/backup.env 2>/dev/null; set +a
while IFS= read -r f; do
  nome=$(basename "$f")
  r=$(timeout 60 ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no -p "${BACKUP_REMOTE_PORT:-23}" \
        -i "${BACKUP_SSH_KEY:-/root/.ssh/id_ed25519_storagebox}" \
        "${BACKUP_REMOTE_USER}@${BACKUP_REMOTE_HOST}" "stat -c %s ${BACKUP_REMOTE_PATH:-nivult}/$nome" 2>/dev/null)
  l=$(stat -c %s "$f")
  if [ -n "$r" ] && [ "$r" = "$l" ]; then
    rm -f "$f"; dire "backup $nome gia' sullo spazio remoto e identico: rimosso da Hetzner ($((l/1048576)) MB)"
  else
    dire "backup $nome NON confermato sul remoto: lo lascio"
  fi
done < <(find /opt/nivult/backups -name "nivult-*.sql.gz.enc" -mtime +$GIORNI_BACKUP -type f | sort)

# 3. modelli e dataset raffreddati
while IFS= read -r f; do sposta "$f" gpu; done \
  < <(find /opt/nivult/gpu -maxdepth 1 -type f \( -name "*.jsonl.gz" -o -name "*.gguf" -o -name "*.pt" \) -mtime +$GIORNI_GPU | sort)

# 4. i MODELLI freddi. Lo script muoveva solo i file di dati e lasciava indietro le
# cartelle dei modelli: il 18/09/2026 il disco e' arrivato al 93% per quelle. Si
# spostano quelle non toccate da GIORNI_MODELLI giorni, tranne quelle in produzione.
GIORNI_MODELLI=${GIORNI_MODELLI:-3}
if [ -x /opt/nivult/gpu/archivia-modelli.sh ]; then
  freddi=$(find /opt/nivult/gpu -maxdepth 1 -type d -mtime +$GIORNI_MODELLI              -not -name gpu -not -name mt5-epoca2 -not -name v1-200k -not -name tecnologie-v1 2>/dev/null)
  [ -n $freddi ] && /opt/nivult/gpu/archivia-modelli.sh $freddi >> $LOG 2>&1
fi

dopo=$(df --output=avail -BG / | tail -1 | tr -dc 0-9)
dire "fine: disco libero ${prima}G -> ${dopo}G"
