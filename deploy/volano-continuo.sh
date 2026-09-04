#!/usr/bin/env bash
# IL VOLANO CONTINUO — il ciclo di crescita che nei grandi player non dorme.
#
# Prima girava solo di notte; ora ogni ~10 minuti:
#   detector   — dominio azienda -> ATS rilevato (i domini in attesa)
#   vanity     — ATS rilevato -> tenant verificato (solo con offerte vere)
#   expira     — un'offerta non rivista da 3 giorni scade: scadenze oneste
#   potatura   — i tenant 404 escono dal giro (rotazione, lotto piccolo)
#   riscoperta — (ogni ora) i domini dei datori dalle offerte nuove
#
# Ogni passo e' incrementale e idempotente: se non c'e' nulla da fare,
# costa una query. Il notturno resta per i lavori pesanti (wikidata, CC,
# render-detector, dedup) dove la latenza non conta.
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"

POTA=(lever greenhouse workable smartrecruiters ashby breezy recruitee jobsoid recruiterbox niceboard personio)
i=0
while true; do
  # riscoperta ogni ~ora: la scansione del raw costa, i domini nuovi
  # arrivano col ritmo delle offerte nuove
  if [ $((i % 6)) -eq 0 ]; then
    "$PY" -m nivult.ats.riscoperta 2>&1 | tail -1 || true
  fi
  "$PY" -m nivult.ats.detector --rileva --limite 400 --thread 20 2>&1 \
    | tail -1 || true
  "$PY" -m nivult.ats.risolutore_vanity --limite 120 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.mantenimento --expira 2>&1 | tail -1 || true
  pid=${POTA[$((i % ${#POTA[@]}))]}
  "$PY" -m nivult.ats.potatura --piattaforma "$pid" --limite 300 2>&1 \
    | tail -1 || true
  i=$((i + 1))
  sleep 600
done
