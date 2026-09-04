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
while true; do
  "$PY" -m nivult.ats.runner --solo-attivi --limite 2000 --thread 30 2>&1 \
    | grep "scrape:" || true
  sleep 5
done
