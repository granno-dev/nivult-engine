#!/bin/bash
# Demone che recupera il TESTO delle offerte SmartRecruiters.
# Le offerte entrano dall'elenco SENZA descrizione; `companies/{azienda}/postings/{id}`
# la restituisce intera, insieme a contratto, livello e remoto dichiarati.
# Il 16/09/2026 mancava il testo a 327.707 offerte attive, mai nemmeno tentate:
# il recupero generico (`arricchisci --dettaglio`, 5.000 per giro sul N5) non ci arrivava.
set -uo pipefail
BASE=/opt/nivult/engine; PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
export MIN_GB_LIBERI="${MIN_GB_LIBERI:-2.5}"
cd "$BASE"
choom -n 500 -p $$ >/dev/null 2>&1 || true
exec nice -n 10 "$PY" /opt/nivult/recupera_smartrecruiters.py --continuo --par "${PAR_TESTI:-20}"
