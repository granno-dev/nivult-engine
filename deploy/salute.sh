#!/usr/bin/env bash
# Il guardiano: gira dopo il notturno e ogni 6 ore. Manda un'email
# all'operatore solo se qualcosa e' rotto. Silenzio = tutto bene.
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
export DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult"
cd "$BASE"
"$PY" -m nivult.ats.salute
