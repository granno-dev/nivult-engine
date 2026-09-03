#!/usr/bin/env bash
# Recupera il paese delle offerte europee rimaste: prima dalla localita'
# (veloce, per chi la localita' ce l'ha gia'), poi Phenom dal dettaglio
# a fette (per chi la localita' non ce l'ha). Gira come servizio: non
# muore alla chiusura di una sessione, e l'arricchimento scrive nel DB
# man mano (UPDATE), quindi niente lavoro perso a meta'.
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"

echo "=== arricchimento paese $(date -Is) ==="
echo "── da-localita (le europee con localita' compilata)"
"$PY" -m nivult.ats.arricchisci --da-localita

echo "── phenom dal dettaglio, a fette da 3000"
for i in $(seq 1 15); do
  out=$("$PY" -m nivult.ats.arricchisci --phenom --limite 3000 --thread 10 2>&1 | tail -1)
  echo "  giro $i: $out"
  echo "$out" | grep -qE "'paesi': 0|0 offerte" && { echo "  niente piu' da fare"; break; }
done

echo "── da-azienda (paese dominante, per il residuo con evidenza)"
"$PY" -m nivult.ats.arricchisci --da-azienda
echo "=== fine $(date -Is) ==="
