#!/bin/bash
# Il cacciatore di domini, trasferito su Hetzner il 19/09/2026.
#
# PERCHE' se n'e' andato da casa. Sul N5 lavorava sulla rete di casa, e HomeShield
# del router bloccava le sue verifiche segnalandole come malware — a ragione, in
# qualche caso: i siti di piccole aziende vengono compromessi di continuo. Ma un
# blocco del router, per noi, e' indistinguibile da un sito irraggiungibile:
# l'azienda finiva archiviata come «provata e fallita» e per regola non si
# riprovava per 30 giorni. Il router ci stava falsificando l'archivio (4.557
# aziende rimesse in coda a mano).
#
# E il trasloco NON e' un compromesso: misurato motore per motore, da qui si
# ricevono PIU' risultati che da casa.
#
#   motore       N5 (casa)   Hetzner
#   google          10         10
#   yahoo            7          7
#   brave            0         16
#   mojeek           0         39
#   startpage        0         39
#   presearch        0         39
#
# L'IP di casa era bruciato dal nostro stesso traffico: Google lo aveva messo in
# sospensione («unusual traffic from your network»).
set -u
cd /opt/nivult
LOG=/opt/nivult/logs/caccia_domini.log
mkdir -p /opt/nivult/logs

export ATS_DATABASE_URL="postgresql://nivult:$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)@127.0.0.1:5432/nivult_ats"
# SearXNG gira qui accanto, legato a 127.0.0.1: non e' esposto a Internet
export SEARX_URL="http://127.0.0.1:8899/search"
# tutti i motori che da questo IP rispondono davvero, misurati
export MOTORI_SEARX="${MOTORI_SEARX:-google,yahoo,brave,mojeek,startpage,presearch}"

while true; do
  if ! curl -s -m 8 -o /dev/null "http://127.0.0.1:8899/"; then
    echo "$(date -u +%FT%TZ) attenzione: searxng non risponde, resto sulle altre fonti" >> "$LOG"
  fi
  echo "$(date -u +%FT%TZ) avvio caccia_domini (motori: $MOTORI_SEARX)" >> "$LOG"
  /opt/nivult/engine/.venv/bin/python /opt/nivult/engine/scripts/caccia_domini.py \
    --continuo --limite 120 --par "${PAR_CACCIA:-6}" >> "$LOG" 2>&1
  echo "$(date -u +%FT%TZ) caccia_domini uscito ($?), riparto fra 60s" >> "$LOG"
  sleep 60
done
