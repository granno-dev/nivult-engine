#!/usr/bin/env bash
# Retention locale degli snapshot: metadata mancanti o illeggibili bloccano tutto.
set -euo pipefail

ENGINE_DIR=${ENGINE_DIR:-/opt/nivult/engine}
EXPORT_DIR=${EXPORT_DIR:-/opt/nivult/exports}
API_CLIENTI_DB=${API_CLIENTI_DB:-/mnt/HC_Volume_106941692/api-clienti.duckdb}

cd "$ENGINE_DIR"
exec .venv/bin/python -m nivult.api_clienti.retention \
  --exports "$EXPORT_DIR" --db "$API_CLIENTI_DB" --apply
