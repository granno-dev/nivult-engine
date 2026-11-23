#!/usr/bin/env bash
set -euo pipefail

BASE="${BASE:-/opt/mavult}"
ENGINE="${ENGINE:-$BASE/engine}"
ENV_FILE="${ENV_FILE:-$BASE/.env}"
DB_CONTAINER="${DB_CONTAINER:-nivult-db-1}"

mkdir -p "$BASE"

if [ ! -f "$ENV_FILE" ]; then
  APP_PW=$(openssl rand -hex 24)
  MIGRATOR_PW=$(openssl rand -hex 24)
  ATS_PW=$(openssl rand -hex 24)
  CV_KEK=$(openssl rand -hex 32)
  umask 077
  cat > "$ENV_FILE" <<EOF
PRODUCT_NAME=Mavult
SERVICE_NAME=mavult-api
API_URL=https://api.mavult.com
SITE_URL=https://mavult.com
SUPPORT_EMAIL=support@mavult.com
DEFAULT_DELIVERY_CHANNELS=app
B2B_ENABLED=0
EXPORT_DIR=${BASE}/exports
NATIVE_CALLBACK=mavult://oauth/callback
CORS_ORIGINS=https://mavult.com

DATABASE_URL=postgresql://mavult_app:${APP_PW}@127.0.0.1:5432/mavult
MIGRATOR_DATABASE_URL=postgresql://mavult_migrator:${MIGRATOR_PW}@127.0.0.1:5432/mavult
ATS_DATABASE_URL=postgresql://mavult_ats_read:${ATS_PW}@127.0.0.1:5432/nivult_ats

CV_KEK=${CV_KEK}
S3_BUCKET=mavult-cvs
S3_ENDPOINT=
S3_ACCESS_KEY=
S3_SECRET_KEY=

GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
MICROSOFT_CLIENT_ID=
MICROSOFT_CLIENT_SECRET=

GLM_API_KEY=
GLM_BASE_URL=https://api.z.ai/api/paas/v4
MISTRAL_API_KEY=
MISTRAL_BASE_URL=https://api.mistral.ai/v1
FANTASTIC_API_KEY=

SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
SMTP_FROM=support@mavult.com
TELEGRAM_BOT_TOKEN=
TELEGRAM_BOT_USERNAME=
TELEGRAM_WEBHOOK_SECRET=
WUZAPI_URL=
WUZAPI_TOKEN=
WUZAPI_NUMBER=
EOF
fi
chmod 600 "$ENV_FILE"

# Anche un .env creato da una versione precedente riceve i vincoli Mavult.
# Cambiano i default dei NUOVI account: nessuna riga users viene convertita.
imposta_env() {
  local chiave="$1" valore="$2" temporaneo
  temporaneo=$(mktemp "$ENV_FILE.tmp.XXXXXX")
  awk -v chiave="$chiave" -v valore="$valore" '
    BEGIN { trovata = 0 }
    $0 ~ "^" chiave "=" {
      if (!trovata) print chiave "=" valore
      trovata = 1
      next
    }
    { print }
    END { if (!trovata) print chiave "=" valore }
  ' "$ENV_FILE" > "$temporaneo"
  mv "$temporaneo" "$ENV_FILE"
  chmod 600 "$ENV_FILE"
}
imposta_env PRODUCT_NAME Mavult
imposta_env SUPPORT_EMAIL support@mavult.com
imposta_env DEFAULT_DELIVERY_CHANNELS app
imposta_env B2B_ENABLED 0
imposta_env EXPORT_DIR "$BASE/exports"

APP_PW=$(awk -F'postgresql://mavult_app:' 'NF>1 {print $2}' "$ENV_FILE" | cut -d@ -f1)
MIGRATOR_PW=$(awk -F'postgresql://mavult_migrator:' 'NF>1 {print $2}' "$ENV_FILE" | cut -d@ -f1)
ATS_PW=$(awk -F'postgresql://mavult_ats_read:' 'NF>1 {print $2}' "$ENV_FILE" | cut -d@ -f1)
[ "${#APP_PW}" -ge 20 ]
[ "${#MIGRATOR_PW}" -ge 20 ]
[ "${#ATS_PW}" -ge 20 ]

docker exec -i "$DB_CONTAINER" psql -U nivult -d postgres -v ON_ERROR_STOP=1 <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mavult_app') THEN
    CREATE ROLE mavult_app LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mavult_migrator') THEN
    CREATE ROLE mavult_migrator LOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mavult_ats_read') THEN
    CREATE ROLE mavult_ats_read LOGIN;
  END IF;
END
\$\$;
ALTER ROLE mavult_app WITH PASSWORD '$APP_PW';
ALTER ROLE mavult_migrator WITH PASSWORD '$MIGRATOR_PW';
ALTER ROLE mavult_ats_read WITH PASSWORD '$ATS_PW';
SELECT 'CREATE DATABASE mavult OWNER mavult_migrator'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'mavult')\gexec
GRANT CONNECT ON DATABASE mavult TO mavult_app, mavult_migrator;
SQL

cd "$ENGINE"

ripristina_in_uscita() {
  stato=$?
  trap - EXIT
  set +e
  docker exec -i "$DB_CONTAINER" psql -U nivult -d postgres -v ON_ERROR_STOP=1 \
    -c 'ALTER ROLE mavult_migrator WITH NOSUPERUSER'
  esito_ripristino=$?
  if [ "$esito_ripristino" -ne 0 ]; then
    printf '%s\n' 'ERRORE: ripristino NOSUPERUSER fallito; verifica manuale obbligatoria' >&2
    if [ "$stato" -eq 0 ]; then
      stato="$esito_ripristino"
    fi
  fi
  exit "$stato"
}

# Da qui fino alla verifica ogni uscita, errore o segnale ripristina il ruolo.
trap ripristina_in_uscita EXIT
docker exec -i "$DB_CONTAINER" psql -U nivult -d postgres -v ON_ERROR_STOP=1 \
  -c 'ALTER ROLE mavult_migrator WITH SUPERUSER'
PYTHONPATH=src MIGRATOR_DATABASE_URL="postgresql://mavult_migrator:${MIGRATOR_PW}@127.0.0.1:5432/mavult" \
  .venv/bin/python -m nivult.migrate up
docker exec -i "$DB_CONTAINER" psql -U nivult -d postgres -v ON_ERROR_STOP=1 \
  -c 'ALTER ROLE mavult_migrator WITH NOSUPERUSER'
rolsuper=$(docker exec -i "$DB_CONTAINER" psql -U nivult -d postgres -v ON_ERROR_STOP=1 \
  -tAc "SELECT rolsuper FROM pg_roles WHERE rolname = 'mavult_migrator'")
if [ "$rolsuper" != "f" ]; then
  printf '%s\n' "ERRORE: mavult_migrator rolsuper=$rolsuper (atteso false)" >&2
  exit 1
fi
trap - EXIT

docker exec -i "$DB_CONTAINER" psql -U nivult -d mavult -v ON_ERROR_STOP=1 <<'SQL'
REVOKE CONNECT ON DATABASE mavult FROM PUBLIC, nivult_app, nivult_migrator;
GRANT CONNECT ON DATABASE mavult TO mavult_app, mavult_migrator;
REASSIGN OWNED BY nivult_migrator TO mavult_migrator;
ALTER SCHEMA public OWNER TO mavult_migrator;
GRANT USAGE ON SCHEMA public TO mavult_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO mavult_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO mavult_app;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO mavult_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mavult_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO mavult_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mavult_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO mavult_app;
ALTER DEFAULT PRIVILEGES FOR ROLE mavult_migrator IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO mavult_app;
SQL

CONTAINER="$DB_CONTAINER" "$ENGINE/deploy/ponte-ats-grants.sh" --mavult
CONTAINER="$DB_CONTAINER" "$ENGINE/deploy/ponte-ats-grants.sh" --mavult --check

printf '%s\n' 'Provisioning Mavult completato: database, ruoli minimi e configurazione B2C'
