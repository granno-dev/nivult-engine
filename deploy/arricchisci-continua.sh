#!/usr/bin/env bash
# L'arricchimento paese che non dorme: da-localita (paese scritto nel
# testo) + da-geonames (la citta' geocodificata, per QUALSIASI paese, non
# solo l'Europa) + phenom dal dettaglio a fette, in loop, cosi' le
# offerte nuove hanno il paese in fretta e il ponte le puo' portare ai
# cluster. Nessuno di questi passi azzera: riempiono solo cio' che manca.
set -uo pipefail
BASE=/opt/nivult/engine; PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
while true; do
  "$PY" -m nivult.ats.arricchisci --da-localita 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --francetravail 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --da-geonames --limite 200000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --workday --limite 200000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --phenom --limite 3000 --thread 10 2>&1 | tail -1 || true
  sleep 300
done
