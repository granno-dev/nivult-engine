#!/usr/bin/env bash
# L'operaio a casa: un container Ubuntu sul N5 (Unraid) che esegue i lotti
# pesanti del motore contro il database di Hetzner attraverso Tailscale.
# Rete dell'host (per vedere tailscale0), memoria e CPU limitate cosi'
# Plex, Nextcloud e il resto di casa non se ne accorgono.
#
# Il ciclo (deploy/operaio-loop.sh) E' il processo principale del
# container: se Unraid o il container si riavviano, riparte da solo
# (`--restart unless-stopped`). Python e le dipendenze stanno nell'immagine
# `nivult-operaio:base`, fatta con `docker commit` dal container preparato
# da operaio-setup.sh: ricreare il container non li perde.
#
# Uso (sul N5): /root/operaio-n5.sh            crea/ricrea e avvia
#               /root/operaio-n5.sh --cpus 12  con un altro tetto di CPU
set -euo pipefail
DIR=/mnt/cache/appdata/nivult-operaio
CPUS=0.5          # mezzo core di default: la ventola di casa ha un padrone
[ "${1:-}" = "--cpus" ] && CPUS="$2"
mkdir -p "$DIR/engine/logs"

if ! docker image inspect nivult-operaio:base >/dev/null 2>&1; then
  if docker ps -a --format '{{.Names}}' | grep -qx nivult-operaio; then
    echo "fotografo il container preparato -> immagine nivult-operaio:base"
    docker commit nivult-operaio nivult-operaio:base >/dev/null
  else
    echo "manca l'immagine nivult-operaio:base: lancia prima operaio-setup.sh dal Mac"; exit 1
  fi
fi

if docker ps -a --format '{{.Names}}' | grep -qx nivult-operaio; then
  docker rm -f nivult-operaio >/dev/null
fi
# il ciclo arriva col checkout git (push dal Mac su engine.git), non da /root
chmod +x "$DIR/engine/deploy/operaio-loop.sh"
docker run -d --name nivult-operaio --network host --restart unless-stopped \
  --memory 48g --cpus "$CPUS" -v "$DIR:/opt/nivult" \
  -e DEBIAN_FRONTEND=noninteractive nivult-operaio:base \
  bash -c 'exec /opt/nivult/engine/deploy/operaio-loop.sh >> /opt/nivult/engine/logs/operaio.log 2>&1' >/dev/null
sleep 3
docker ps --format '{{.Names}} {{.Status}} cpus='"$CPUS" | grep nivult-operaio
docker exec nivult-operaio bash -c 'ps -eo pid,etime,cmd | grep -E "operaio-loo[p]|[.]venv/bin/python"'
