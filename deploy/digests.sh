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

BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
LOCK=/var/run/nivult-digests.lock

log() { echo "$(date -Is) $*"; }

exec 9>"$LOCK"
if ! flock -n 9; then
  log "un giro precedente è ancora in corso: salto"
  exit 0
fi

cd "$BASE" || { log "ERRORE: $BASE non accessibile"; exit 1; }

log "=== digest ==="
if ! "$PY" -m nivult.matching.worker; then
  log "ERRORE: il worker dei digest è uscito con errore"
  exit 1
fi
log "=== fine ==="
