#!/usr/bin/env bash
# Lo scrape che non dorme: a lotti di 500, in continuo, dando priorita'
# alle aziende mai viste (le 47mila del tesoro) e poi alle attive piu'
# stantie — cosi' le vive restano fresche come col polling di Fantastic.
# Gira come servizio: quando la coda delle mai-viste e' vuota, continua
# a rinfrescare le attive, sempre a partire da chi ne ha piu' bisogno.
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
while true; do
  "$PY" -m nivult.ats.runner --limite 500 --thread 16 2>&1 \
    | grep "scrape:" || true
  # una pausa breve tra i lotti: gentilezza verso le piattaforme
  # condivise (greenhouse, lever...) per non farsi limitare
  sleep 20
done
