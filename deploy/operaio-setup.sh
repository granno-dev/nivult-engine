#!/usr/bin/env bash
# Mette in piedi l'OPERAIO A CASA, dal Mac di Giuseppe, in un colpo solo.
# (Deciso il 2026-09-06: il N5 su Unraid fa i lotti pesanti, Hetzner tiene
# API, database e digest.) Idempotente: si puo' rilanciare.
#
#   1. sul server Hetzner: Postgres anche sull'indirizzo Tailscale
#      (riavvio del db di pochi secondi; lo sprint GLM va rilanciato dopo)
#   2. codice del motore e segreti copiati nel container del N5
#   3. Python, dipendenze, torch CPU dentro il container
#   4. prova: il N5 conta le offerte nel database di Hetzner
set -euo pipefail
SERVER=root@37.27.36.85
N5=root@100.119.200.7
IP_TS_SERVER=100.117.204.17
QUI=$(cd "$(dirname "$0")" && pwd)

echo "== 1) Postgres sull'indirizzo Tailscale del server"
scp -q "$QUI/apri-db-tailscale.sh" "$SERVER:/opt/nivult/engine/deploy/"
ssh "$SERVER" 'chmod 700 /opt/nivult/engine/deploy/apri-db-tailscale.sh && /opt/nivult/engine/deploy/apri-db-tailscale.sh'

echo "== 2) container sul N5, codice e segreti"
scp -q "$QUI/operaio-n5.sh" "$N5:/root/operaio-n5.sh"
ssh "$N5" 'chmod 700 /root/operaio-n5.sh && /root/operaio-n5.sh'
ssh "$SERVER" 'tar czf - -C /opt/nivult engine --exclude=engine/.venv --exclude=engine/logs --exclude=engine/.git --exclude="*.pyc" --exclude="__pycache__"' \
  | ssh "$N5" 'tar xzf - -C /mnt/cache/appdata/nivult-operaio && du -sh /mnt/cache/appdata/nivult-operaio/engine'
ssh "$SERVER" 'grep -h -E "^(POSTGRES_PASSWORD|GLM_API_KEY|GLM_BASE_URL|GROQ_API_KEY|MISTRAL_API_KEY|NVIDIA_API_KEY|BRANDFETCH_CLIENT_ID)=" /opt/nivult/.env /opt/nivult/engine/.env 2>/dev/null | sort -u' \
  | ssh "$N5" "umask 077; D=/mnt/cache/appdata/nivult-operaio; cat > \$D/.env
PW=\$(grep -E '^POSTGRES_PASSWORD=' \$D/.env | cut -d= -f2-)
echo \"ATS_DATABASE_URL=postgresql://nivult:\${PW}@$IP_TS_SERVER:5432/nivult_ats\" >> \$D/.env
echo \"DATABASE_URL=postgresql://nivult:\${PW}@$IP_TS_SERVER:5432/nivult\" >> \$D/.env
chmod 600 \$D/.env; echo \"segreti: \$(wc -l < \$D/.env) righe, permessi 600\""
ssh "$N5" 'mkdir -p /mnt/cache/appdata/nivult-operaio/engine/logs'

echo "== 3) Python e dipendenze nel container (qualche minuto)"
ssh "$N5" 'docker exec -e DEBIAN_FRONTEND=noninteractive nivult-operaio bash -c "
  apt-get update -qq > /opt/nivult/apt.log 2>&1
  apt-get install -y -qq python3.12 python3.12-venv python3-pip build-essential git curl iputils-ping >> /opt/nivult/apt.log 2>&1
  cd /opt/nivult/engine
  [ -x .venv/bin/python ] || python3.12 -m venv .venv
  .venv/bin/pip install -q --upgrade pip > /opt/nivult/pip.log 2>&1
  .venv/bin/pip install -q -e . >> /opt/nivult/pip.log 2>&1
  .venv/bin/pip install -q torch --index-url https://download.pytorch.org/whl/cpu >> /opt/nivult/pip.log 2>&1
  .venv/bin/pip install -q transformers sentencepiece >> /opt/nivult/pip.log 2>&1
  echo PIP_OK >> /opt/nivult/pip.log
  .venv/bin/python -c \"import nivult, torch, transformers; print(\\\"python ok, torch\\\", torch.__version__)\"
"'

echo "== 4) prova: il N5 legge il database di Hetzner via Tailscale"
ssh "$N5" 'docker exec nivult-operaio bash -c "
  set -a; . /opt/nivult/.env; set +a
  cd /opt/nivult/engine && .venv/bin/python - <<EOF
import os, time, psycopg
t=time.time()
with psycopg.connect(os.environ[\"ATS_DATABASE_URL\"], connect_timeout=10) as c:
    n=c.execute(\"SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL\").fetchone()[0]
print(f\"offerte attive viste dal N5: {n}  (andata e ritorno {time.time()-t:.2f}s)\")
EOF
"'
echo "== FATTO: l'operaio e' pronto. Ora rilancia lo sprint GLM sul server."
