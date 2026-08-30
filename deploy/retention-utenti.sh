#!/usr/bin/env bash
#
# La conservazione dei dati personali, una volta al giorno.
#
# Gira alle 04:30: dopo il backup delle 03:00 e il riavvio delle 04:00, e
# PRIMA del ponte ATS delle 05:00. L'ordine non e' estetico — una
# cancellazione va sempre dopo un backup fresco, cosi' se un termine e'
# sbagliato i dati sono ancora recuperabili dal dump della notte.
#
# `flock` perche' un secondo giro sovrapposto proverebbe a cancellare
# utenti che il primo sta gia' cancellando.
set -euo pipefail

BASE=/opt/nivult/engine
exec /usr/bin/flock -n /var/lock/nivult-retention-utenti.lock \
  "$BASE/.venv/bin/python" -m nivult.retention_utenti "$@"
