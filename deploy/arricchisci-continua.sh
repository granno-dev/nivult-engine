#!/usr/bin/env bash
# L'arricchimento paese che non dorme: da-localita (europee con
# localita') + phenom dal dettaglio a fette, in loop, cosi' le offerte
# nuove hanno il paese in fretta e il ponte le puo' portare ai cluster.
set -uo pipefail
BASE=/opt/nivult/engine; PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
while true; do
  "$PY" -m nivult.ats.arricchisci --da-localita 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --phenom --limite 3000 --thread 10 2>&1 | tail -1 || true
  sleep 300
done
