#!/usr/bin/env bash
#
# Digest orari: per ogni utente il cui slot è scaduto, valuta e consegna.
#
# Ogni ora e non una volta al giorno perché l'orario di invio è quello
# dell'utente (send_hour_local, nel SUO fuso): un giro solo alle 01:00
# consegnerebbe i digest di mezzanotte con ore di ritardo. Il worker stesso
# decide chi è dovuto: senza utenti dovuti il giro costa una query.
#
# flock: se un giro è ancora in corso, questo NON parte. Un digest lento non
# deve sovrapporsi al suo stesso rilancio.
#
# Richiede SMTP_HOST e SMTP_FROM nell'ambiente per la consegna vera; senza,
# i digest falliscono con motivo esplicito e restano visibili negli allarmi.

set -uo pipefail

BASE="${BASE:-/opt/nivult/engine}"
PY="$BASE/.venv/bin/python"
LOCK="${LOCK:-/var/run/nivult-digests.lock}"

log() { echo "$(date -Is) $*"; }

exec 9>"$LOCK"
# 26/09/2026: come nightly — flock con attesa e uscita rumorosa, non
# un'uscita 0 in silenzio che nasconde un giro impuntato.
if ! flock -w 10800 9; then
  log "un giro precedente e' ancora in corso dopo 3h: esco CON ERRORE"
  exit 1
fi

cd "$BASE" || { log "ERRORE: $BASE non accessibile"; exit 1; }
if [ -n "${ENV_FILE:-}" ]; then
  [ -f "$ENV_FILE" ] || { log "ERRORE: $ENV_FILE non accessibile"; exit 1; }
  set -a
  . "$ENV_FILE"
  set +a
fi
export MAESTRO_URL="${MAESTRO_URL:-http://100.119.200.7:8090}"
export PRESCORE_V3_URL="${PRESCORE_V3_URL:-http://100.119.200.7:8095}"
export PRESCORE_V3_ADAPTIVE="${PRESCORE_V3_ADAPTIVE:-1}"
export PRESCORE_V3_ANN="${PRESCORE_V3_ANN:-1}"

log "=== digest ==="
if [ "${PRESCORE_V3_ANN:-0}" = "1" ]; then
  if ! "$PY" scripts/aggiorna_embedding_v3.py --limite 256 --produzione; then
    log "ERRORE: aggiornamento cache embedding non riuscito; controllo copertura prima dell'ANN"
  fi
fi
if ! "$PY" -m nivult.matching.worker; then
  log "ERRORE: il worker dei digest è uscito con errore"
  exit 1
fi
log "=== fine ==="
