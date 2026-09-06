#!/usr/bin/env bash
# L'operaio a casa: un container Ubuntu sul N5 (Unraid) che esegue i lotti
# pesanti del motore contro il database di Hetzner attraverso Tailscale.
# Rete dell'host (per vedere tailscale0), 48 GB e 16 thread al massimo
# cosi' Plex, Nextcloud e il resto di casa non se ne accorgono.
# Idempotente: se il container esiste, lo lascia.
set -euo pipefail
DIR=/mnt/cache/appdata/nivult-operaio
mkdir -p "$DIR"
if ! docker ps -a --format '{{.Names}}' | grep -qx nivult-operaio; then
  docker run -d --name nivult-operaio --network host --restart unless-stopped \
    --memory 48g --cpus 16 -v "$DIR:/opt/nivult" \
    -e DEBIAN_FRONTEND=noninteractive ubuntu:24.04 sleep infinity >/dev/null
  echo "container creato"
fi
docker start nivult-operaio >/dev/null 2>&1 || true
docker ps --format '{{.Names}} {{.Status}}' | grep nivult-operaio
