#!/usr/bin/env bash
# Esegue un modulo del motore DENTRO il container dell'operaio sul N5,
# con i segreti dell'operaio (database di Hetzner via Tailscale) e il log
# in /mnt/cache/appdata/nivult-operaio/engine/logs/<nome>.log.
#
# Uso (sul N5):  /root/operaio.sh <nome-log> <modulo> [argomenti...]
#   /root/operaio.sh estrai-extra nivult.ats.estrai_extra --limite 100000
#
# flock: lo stesso nome non gira due volte insieme. nice: Plex e casa
# hanno la precedenza sull'operaio.
set -uo pipefail
NOME=$1; shift
docker exec -d nivult-operaio bash -c "
  set -a; . /opt/nivult/.env; set +a
  cd /opt/nivult/engine
  exec flock -n /tmp/nivult-$NOME.lock nice -n 10 .venv/bin/python -m $* >> logs/$NOME.log 2>&1
"
echo "avviato $NOME ($*) -> logs/$NOME.log"
