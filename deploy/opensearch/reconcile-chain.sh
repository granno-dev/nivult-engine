#!/usr/bin/env bash
set -euo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"

attendi() {
  local unita
  for unita in "$@"; do
    while systemctl is-active --quiet "$unita"; do
      sleep 30
    done
    if systemctl is-failed --quiet "$unita"; then
      echo "$unita fallita: interrompo la catena" >&2
      systemctl status "$unita" --no-pager || true
      exit 1
    fi
  done
}

avvia() {
  local i="$1"
  systemd-run --unit="nivult-reconcile-$i" --collect --quiet \
    --property=WorkingDirectory="$BASE" \
    --property=EnvironmentFile=/opt/nivult/.env \
    --property=EnvironmentFile=/etc/systemd/system/nivult-api.env \
    "$PY" "$BASE/scripts/opensearch_sync.py" --reconcile --worker "$i" --workers 6
}

attendi nivult-reconcile-0 nivult-reconcile-1 nivult-reconcile-2
for i in 3 4 5; do
  avvia "$i"
done
attendi nivult-reconcile-3 nivult-reconcile-4 nivult-reconcile-5

curl -fsS -X PUT 'http://127.0.0.1:9200/nivult_jobs_active/_settings' \
  -H 'Content-Type: application/json' \
  --data '{"index":{"refresh_interval":"5s"}}'
curl -fsS -X POST 'http://127.0.0.1:9200/nivult_jobs_active/_refresh'
systemctl enable --now nivult-opensearch-sync@1 nivult-opensearch-sync@2 \
  nivult-opensearch-sync@3 nivult-opensearch-sync@4
