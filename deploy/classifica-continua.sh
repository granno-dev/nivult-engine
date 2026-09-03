#!/usr/bin/env bash
# La classificazione che non dorme: tiene il passo con lo scrape
# continuo, cosi' le offerte nuove diventano usabili in minuti, non
# alla notte. A lotti da 20000, con la scrittura incrementale gia' a
# prova di riavvio. --no-glm: solo i livelli gratuiti (dizionario +
# codici occupazione), il residuo ostico lo prende il GLM notturno.
set -uo pipefail
BASE=/opt/nivult/engine; PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
while true; do
  "$PY" -m nivult.ats.classificatore_livelli --no-glm --limite 20000 2>&1 \
    | grep -E "classificate|viste" | tail -1 || true
  sleep 180
done
