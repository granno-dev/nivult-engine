#!/usr/bin/env bash
# I CERTIFICATI — il flusso Certificate Transparency filtrato sui
# sottodomini di assunzione: careers.*, jobs.*, karriere.*, werkenbij*…
# Ogni dominio trovato entra in company_domains (source=certificati) e
# da li' e' filiera collaudata: detector -> vanity -> offerte.
set -uo pipefail
BASE=/opt/nivult/engine
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
exec "$BASE/.venv/bin/python" -m nivult.ats.certificati --demone
