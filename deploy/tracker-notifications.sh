#!/usr/bin/env bash
# Promemoria del tracker; il worker deduplica per evento e riprova i fallimenti.
set -uo pipefail

BASE="${BASE:-/opt/nivult/engine}"
PY="$BASE/.venv/bin/python"
LOCK="${LOCK:-/var/run/nivult-tracker-notifications.lock}"

log() { echo "$(date -Is) $*"; }

exec 9>"$LOCK"
if ! flock -w 900 9; then
  log "un giro precedente e' ancora in corso dopo 15m: esco CON ERRORE"
  exit 1
fi

cd "$BASE" || { log "ERRORE: $BASE non accessibile"; exit 1; }
if [ -n "${ENV_FILE:-}" ]; then
  [ -f "$ENV_FILE" ] || { log "ERRORE: $ENV_FILE non accessibile"; exit 1; }
  set -a
  . "$ENV_FILE"
  set +a
fi

exec "$PY" -m nivult.tracker_notifications
