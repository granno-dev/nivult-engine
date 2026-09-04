#!/usr/bin/env bash
# L'arricchimento paese che non dorme: da-localita (paese scritto nel
# testo) + da-geonames (la citta' geocodificata, per QUALSIASI paese, non
# solo l'Europa) + phenom dal dettaglio a fette, in loop, cosi' le
# offerte nuove hanno il paese in fretta e il ponte le puo' portare ai
# cluster. Nessuno di questi passi azzera: riempiono solo cio' che manca.
set -uo pipefail
BASE=/opt/nivult/engine; PY="$BASE/.venv/bin/python"
GLM_API_KEY=$(grep -E '^GLM_API_KEY=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export GLM_API_KEY
# GLM_BASE_URL: esportarla VUOTA rompe il client (URL senza protocollo);
# solo se davvero presente nel .env, altrimenti vale il default del codice.
GLM_BASE_URL=$(grep -E '^GLM_BASE_URL=' /opt/nivult/.env | head -1 | cut -d= -f2-)
[ -n "$GLM_BASE_URL" ] && export GLM_BASE_URL
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
while true; do
  "$PY" -m nivult.ats.arricchisci --da-localita 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --francetravail 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --da-geonames --limite 200000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --workday --limite 200000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --phenom --limite 3000 --thread 10 2>&1 | tail -1 || true
  # loghi azienda per la board: consolida i logo per-offerta + og:image
  # della board (ashby/lever/workable/smartrecruiters/greenhouse), a lotti.
  "$PY" -m nivult.ats.loghi --da-offerte 2>&1 | tail -1 || true
  # descrizioni dal dettaglio, per chi non le mette in lista
  "$PY" -m nivult.ats.descrizioni --smartrecruiters --limite 400 2>&1 | tail -1 || true
  # lingua dell'annuncio: deterministica, gratis, dopo le descrizioni
  "$PY" -m nivult.ats.lingua --limite 100000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.descrizioni --workday --limite 1500 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.descrizioni --da-pagina --limite 1500 2>&1 | tail -1 || true
  # profilo: seniority/remote/skill — dizionari gratis + GLM Flash (gratuito)
  # SOLO sul residuo, tetto 400/ciclo: mai credito pagato.
  "$PY" -m nivult.ats.profilo --limite 40000 --glm-max 400 2>&1 | tail -1 || true
  # paese via GLM Flash (gratuito) per il residuo non geocodificabile:
  # accuratezza misurata 29/30; XX/incerto non si salva.
  "$PY" -m nivult.ats.profilo --paese-glm 1500 2>&1 | tail -1 || true
  # salari: estrae min/max/valuta/periodo dal raw (nuove offerte)
  "$PY" -m nivult.ats.salari --limite 30000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.loghi --da-board --limite 600 2>&1 | tail -1 || true
  sleep 300
done
