#!/usr/bin/env bash
# La CORSIA VELOCE: rivisita in continuo SOLO i tenant che hanno offerte
# (~30k), a lotti dei piu' stantii, cosi' le posizioni appena pubblicate
# compaiono in minuti, non in ore — il flusso «live» come Fantastic.
# Gira accanto allo scrape principale (che tiene fresco anche il codone
# vuoto): qui la priorita' e' la freschezza degli ATS gia' vivi.
# Il rate-limiter per-piattaforma nel runner protegge gli endpoint
# condivisi (greenhouse/lever/smartrecruiters/ashby/recruitee) anche con
# 30 thread: gli altri ATS hanno host per-tenant e parallelizzano liberi.
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
# 09/10/2026: stessa auto-guarigione dello scrape principale (motivo la'):
# db che sparisce = runner appeso su connessioni morte mentre il servizio
# sembra vivo. pg_isready tre volte -> esci e systemd riavvia; timeout sul
# lotto -> nessun incaglio dura oltre 60 minuti.
fallimenti_db=0
while true; do
  if ! pg_isready -h 127.0.0.1 -U nivult -d nivult_ats -q; then
    fallimenti_db=$((fallimenti_db + 1))
    if [ "$fallimenti_db" -ge 3 ]; then
      echo "scrape-veloce: db irraggiungibile per 3 giri, esco" >&2
      exit 1
    fi
    sleep 30
    continue
  fi
  fallimenti_db=0
  timeout -k 30 60m "$PY" -m nivult.ats.runner --solo-attivi --limite 2000 --thread 30 2>&1 \
    | grep -E "scrape:|Traceback|Error|error" || true
  sleep 5
done
