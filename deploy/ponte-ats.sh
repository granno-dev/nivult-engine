#!/usr/bin/env bash
#
# Il travaso quotidiano dall'ATS al funnel.
#
# L'orario, e perché è quello: l'ATS gira alle 02:30 e deve aver finito di
# scaricare e classificare; il riavvio automatico è alle 04:00, e un ponte
# a cavallo di un riavvio si troncherebbe; il primo digest utile del
# mattino parte alle 07:10. Le 05:00 stanno dopo entrambi i vincoli e
# prima della consegna, che è l'unica cosa che conta: un'offerta ATS
# travasata alle 05:00 può finire nel digest delle 07:10 dello stesso
# giorno.
#
# flock come per i digest: se un giro è ancora in corso questo non parte.
# Il ponte è riprendibile — `upsert_job` aggiorna invece di duplicare —
# ma due giri sovrapposti si contenderebbero le stesse righe senza
# guadagnare niente.
#
# Non serve nessun segreto in più: `ATS_DATABASE_URL` si deduce da
# `DATABASE_URL` cambiando il nome del database, e `nivult_app` ha sul
# database dell'ATS la sola lettura (vedi `deploy/ponte-ats-grants.sh`).

set -uo pipefail

BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
LOCK=/var/run/nivult-ponte-ats.lock

log() { echo "$(date -Is) $*"; }

exec 9>"$LOCK"
if ! flock -n 9; then
  log "un giro precedente è ancora in corso: salto"
  exit 0
fi

cd "$BASE" || { log "ERRORE: $BASE non accessibile"; exit 1; }

log "=== ponte ATS ==="
if ! "$PY" scripts/ponte_ats.py; then
  log "ERRORE: il ponte ATS è uscito con errore"
  exit 1
fi
log "=== fine ==="
