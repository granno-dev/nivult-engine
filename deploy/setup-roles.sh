#!/usr/bin/env bash
#
# Assegna le credenziali ai ruoli creati dalla migrazione 0010.
#
# La migrazione crea nivult_app e nivult_migrator senza password e senza LOGIN,
# perché in un file versionato non entrano segreti. Le password le mette questo
# script, leggendole dall'ambiente.
#
#   NIVULT_APP_PASSWORD=... NIVULT_MIGRATOR_PASSWORD=... ./setup-roles.sh
#
# Poi in /opt/nivult/.env:
#   DATABASE_URL=postgresql://nivult_app:<password>@127.0.0.1:5432/nivult
#   MIGRATOR_DATABASE_URL=postgresql://nivult_migrator:<password>@127.0.0.1:5432/nivult
#
# L'applicazione usa DATABASE_URL. Solo il runner di migrazioni usa l'altra.

set -euo pipefail

CONTAINER=${CONTAINER:-nivult-db-1}
SUPERUSER=${SUPERUSER:-nivult}
DB=${DB:-nivult}

: "${NIVULT_APP_PASSWORD:?serve NIVULT_APP_PASSWORD}"
: "${NIVULT_MIGRATOR_PASSWORD:?serve NIVULT_MIGRATOR_PASSWORD}"

# Le password passano da variabile d'ambiente e da psql -v, mai sulla riga di
# comando: gli argomenti sono visibili in ps a qualunque utente della macchina.
docker exec -i \
  -e APP_PW="$NIVULT_APP_PASSWORD" \
  -e MIG_PW="$NIVULT_MIGRATOR_PASSWORD" \
  "$CONTAINER" psql -U "$SUPERUSER" -d "$DB" -v ON_ERROR_STOP=1 <<'SQL'
\set app_pw `echo "$APP_PW"`
\set mig_pw `echo "$MIG_PW"`
ALTER ROLE nivult_app      LOGIN PASSWORD :'app_pw';
ALTER ROLE nivult_migrator LOGIN PASSWORD :'mig_pw';
SQL

echo "ruoli configurati. Verifica:"
docker exec "$CONTAINER" psql -U "$SUPERUSER" -d "$DB" -tAc \
  "SELECT rolname || ': login=' || rolcanlogin || ' super=' || rolsuper
     FROM pg_roles WHERE rolname LIKE 'nivult%' ORDER BY rolname"

# il ruolo di lettura dell'API clienti (migrazione 0068) vive su nivult_ats
# e ha la password solo se l'ambiente la porta — opzionale perche' serve
# solo dove gira l'API
if [ -n "${API_LETTURA_PASSWORD:-}" ]; then
  docker exec -i -e LETTURA_PW="$API_LETTURA_PASSWORD" \
    "$CONTAINER" psql -U "$SUPERUSER" -d nivult_ats -v ON_ERROR_STOP=1 <<'SQL'
\set lettura_pw `echo "$LETTURA_PW"`
ALTER ROLE nivult_api_lettura LOGIN PASSWORD :'lettura_pw';
SQL
  echo "nivult_api_lettura: credenziale assegnata (su nivult_ats)"
fi
