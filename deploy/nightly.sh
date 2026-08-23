#!/usr/bin/env bash
#
# Giro notturno: ingestione, poi scadute, poi allarmi.
#
# Orario 01:00, deliberatamente PRIMA delle 03:00 del backup e delle 04:00 del
# riavvio automatico. L'ordine conta: si ingerisce, poi il backup fotografa la
# notte appena fatta, poi la macchina si riavvia. Girare alle 04:00 significava
# farsi interrompere a metà; girare dopo il backup significava un backup sempre
# vecchio di un giorno.
#
# flock: se un giro precedente è ancora in corso, questo NON parte. Due runner
# insieme si contenderebbero il budget e il breaker si aprirebbe per una
# concorrenza che non doveva esistere.

set -uo pipefail

BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
LOCK=/var/run/nivult-nightly.lock
STATE=/opt/nivult/nightly-state

log() { echo "$(date -Is) $*"; }

exec 9>"$LOCK"
if ! flock -n 9; then
  log "un giro precedente è ancora in corso: salto"
  exit 0
fi

cd "$BASE" || { log "ERRORE: $BASE non accessibile"; exit 1; }

esito=0

log "=== ingestione ==="
if ! "$PY" -m nivult.ingestion.runner --all --limit 150; then
  log "ERRORE: il runner è uscito con errore"
  esito=1
fi

log "=== scadute ==="
if ! "$PY" -m nivult.ingestion.sweep; then
  log "ERRORE: lo sweep è uscito con errore"
  esito=1
fi

log "=== allarmi ==="
# --check esce 1 quando c'è qualcosa da guardare: non è un errore del giro,
# è il suo risultato.
if ! "$PY" -m nivult.report --check; then
  log "ci sono allarmi: vedi sopra"
fi

printf '%s\t%s\n' "$([ $esito -eq 0 ] && echo ok || echo failed)" "$(date -Is)" > "$STATE"
log "=== fine (esito $esito) ==="
exit $esito
