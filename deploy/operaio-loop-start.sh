#!/usr/bin/env bash
# Avvia (o riavvia) il ciclo dell'operaio nel container sul N5. Idempotente.
set -uo pipefail
docker exec nivult-operaio bash -c 'pkill -f "deploy/operaio-loo[p].sh" 2>/dev/null; sleep 1' || true
docker cp /root/operaio-loop.sh nivult-operaio:/opt/nivult/engine/deploy/operaio-loop.sh
docker exec -d nivult-operaio bash -c 'chmod +x /opt/nivult/engine/deploy/operaio-loop.sh; exec /opt/nivult/engine/deploy/operaio-loop.sh >> /opt/nivult/engine/logs/operaio.log 2>&1'
sleep 3
docker exec nivult-operaio bash -c 'ps -eo pid,etime,cmd | grep -E "operaio-loo[p]|[.]venv/bin/python" | grep -v grep'
