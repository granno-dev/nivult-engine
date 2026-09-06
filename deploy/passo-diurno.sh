#!/usr/bin/env bash
# Un passo diurno: flock contro le sovrapposizioni, env completo, log suo.
# Uso: passo-diurno.sh <nome-log> <modulo> [argomenti...]
NOME="$1"; shift
BASE=/opt/nivult/engine
POSTGRES_PASSWORD=$(grep -E "^POSTGRES_PASSWORD=" /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
export GLM_API_KEY=$(grep -E "^GLM_API_KEY=" $BASE/.env | cut -d= -f2-)
export BRANDFETCH_CLIENT_ID=$(grep -E "^BRANDFETCH_CLIENT_ID=" /opt/nivult/.env | cut -d= -f2-)
# choom +500: se la memoria finisce, il kernel uccida un passo diurno
# (riprendibile) e mai Postgres o l'API (che stanno a -900)
exec choom -n 500 -- flock -n /tmp/nivult-$NOME.lock \
  "$BASE/.venv/bin/python" -m "$@" >> "$BASE/logs/$NOME.log" 2>&1
