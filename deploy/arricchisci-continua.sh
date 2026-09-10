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
BRANDFETCH_CLIENT_ID=$(grep -E '^BRANDFETCH_CLIENT_ID=' /opt/nivult/.env | head -1 | cut -d= -f2-)
[ -n "$BRANDFETCH_CLIENT_ID" ] && export BRANDFETCH_CLIENT_ID
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
# lotti riprendibili: se la memoria finisce, muoiano loro e non il database
choom -n 500 -p $$ >/dev/null 2>&1 || true
while true; do
  # Finche' gira lo sprint GLM (stessa chiave, 50 chiamate parallele), i
  # passi che chiamano GLM da qui producono solo 429 — per se' e per lo
  # sprint. Misurato il 2026-09-06: glm-extra 3 errori su 3 alle 09:00.
  # /opt/nivult/glm-corpus.spento: l'interruttore di Giuseppe (07/09/2026, credito
  # GLM a zero per scelta): sul corpus GLM non si chiama, nivult-v1 e le
  # regole bastano. Resta acceso solo per i digest e i CV degli utenti.
  if [ -f /opt/nivult/glm-corpus.spento ] || ps -eo cmd | grep -q "python /opt/nivult/sprint_glm[.]py"; then
    GLM_MAX=0; PAESE_GLM=0
  else
    GLM_MAX=400; PAESE_GLM=1500
  fi
  "$PY" -m nivult.ats.arricchisci --da-localita 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --francetravail 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --da-geonames --limite 200000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --workday --limite 200000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.arricchisci --phenom --limite 3000 --thread 10 2>&1 | tail -1 || true
  # loghi azienda per la board: consolida i logo per-offerta + og:image
  # della board (ashby/lever/workable/smartrecruiters/greenhouse), a lotti.
  # ...ma non a ogni giro: i loghi nuovi arrivano col ritmo delle aziende
  # nuove, cioe' lento. Girava in continuazione e teneva il database
  # occupato al 100% (misurato il 10/09/2026). Ora al massimo ogni ora.
  T_LOGHI=/opt/nivult/engine/logs/.loghi-offerte.timbro
  if [ ! -f "$T_LOGHI" ] || [ $(( $(date +%s) - $(stat -c %Y "$T_LOGHI") )) -gt 3600 ]; then
    "$PY" -m nivult.ats.loghi --da-offerte 2>&1 | tail -1 || true
    touch "$T_LOGHI"
  fi
  "$PY" /opt/nivult/engine/deploy/timbra_prima_vista.py 2>&1 | tail -1 || true
  # descrizioni dal dettaglio, per chi non le mette in lista
  "$PY" -m nivult.ats.descrizioni --smartrecruiters --limite 400 2>&1 | tail -1 || true
  # lingua dell'annuncio: deterministica, gratis, dopo le descrizioni
  "$PY" -m nivult.ats.lingua --limite 100000 2>&1 | tail -1 || true
  # tipo di contratto + contatto (estrai_extra): sull'operaio N5 dal 2026-09-06
  "$PY" -m nivult.ats.descrizioni --workday --limite 2500 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.descrizioni --da-pagina --limite 2500 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.descrizioni --da-testo --limite 2500 2>&1 | tail -1 || true
  # profilo: seniority/remote/skill — dizionari gratis + GLM Flash (gratuito)
  # SOLO sul residuo, tetto 400/ciclo: mai credito pagato.
  "$PY" -m nivult.ats.profilo --limite 40000 --glm-max "$GLM_MAX" 2>&1 | tail -1 || true
  # paese via GLM Flash (gratuito) per il residuo non geocodificabile:
  # accuratezza misurata 29/30; XX/incerto non si salva.
  if [ "$PAESE_GLM" -gt 0 ]; then
    "$PY" -m nivult.ats.profilo --paese-glm "$PAESE_GLM" 2>&1 | tail -1 || true
  fi
  # estrai_extra e' passato all'operaio sul N5 (2026-09-06): qui non piu'.
  # salari: estrae min/max/valuta/periodo dal raw (nuove offerte)
  "$PY" -m nivult.ats.salari --limite 30000 2>&1 | tail -1 || true
  "$PY" -m nivult.ats.loghi --da-board --limite 600 2>&1 | tail -1 || true
  # brandfetch a fettine: le aziende NUOVE in bacheca (l'ordine parte da
  # chi pubblica adesso) hanno il logo in minuti, non alla prossima notte
  if [ -n "${BRANDFETCH_CLIENT_ID:-}" ]; then
    "$PY" -m nivult.ats.loghi --da-brandfetch --limite 120 2>&1 | tail -1 || true
  fi
  sleep 300
done
