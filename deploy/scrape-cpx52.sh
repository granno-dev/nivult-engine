#!/usr/bin/env bash
# La corsia della CPX52 per TUTTE le piattaforme senza rate gate che non
# sono gia' assegnate a scrape-continuo.sh. Nata il 15/10/2026 dopo la prova
# BambooHR: il N5 smette lo scrape generale e resta a classificazione,
# scoperta e arricchimento. Le liste tengono separate le tre corsie.
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"

GATE="greenhouse,lever,smartrecruiters,ashby,recruitee,workable,ukg"
GIA_ASSEGNATE="bamboohr,breezy,personio,teamtailor"
SALTA="$GATE,$GIA_ASSEGNATE"

fallimenti_db=0
while true; do
  if ! docker exec nivult-db-1 pg_isready -U nivult -d nivult_ats -q; then
    fallimenti_db=$((fallimenti_db + 1))
    if [ "$fallimenti_db" -ge 3 ]; then
      echo "scrape-cpx52: db irraggiungibile per 3 giri, esco" >&2
      exit 1
    fi
    sleep 30
    continue
  fi
  fallimenti_db=0
  # 12 thread: valore operativo corrente. Si alza solo dopo aver misurato
  # I/O, API ed errori, mai durante un rilascio.
  timeout -k 30 60m "$PY" -m nivult.ats.runner --limite 2000 --thread 12 \
    --salta "$SALTA" 2>&1 | grep -E "scrape:|Traceback|Error|error" || true
  sleep 5
done
