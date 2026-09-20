#!/bin/bash
# Il supervisore del N5. Dal 19/09/2026 qui gira mT5, non piu' il 2B.
#
# La divisione del lavoro decisa il 18/09 e misurata:
#   N5       v1 (tutti i campi strutturati) + mT5 (le sintesi di tutto)
#   Mac mini 2B (le tecnologie di tutto, e il ripasso delle sintesi incerte)
#
# Il 2B NON deve girare qui insieme a mT5: la Radeon 890M non ha memoria propria,
# la prende dalla RAM di sistema, e due motori sulla stessa scheda il 16/09 hanno
# portato il carico a 356 e incastrato la macchina per undici ore.
#
# Fino a stamattina l'estrattore veniva lanciato a mano e quando moriva nessuno se
# ne accorgeva: il 19/09 ha lavorato il solo Mac mini per ore.
set -u
cd /opt/nivult
set -a; . /opt/nivult/.env; set +a
LOG=/opt/nivult/logs/sintesi_mt5.log
mkdir -p /opt/nivult/logs

# La fascia silenziosa, la stessa di operaio-loop.sh e con lo stesso
# interruttore: fra le 23:30 e le 7:00 di Roma mT5 non parte, e se sta girando
# viene fermato. La copertura della fascia prima si fermava al rilevatore, al
# render e a v1; mT5 restava, e da solo teneva la scheda al 99% — quindi la
# ventola non calava mai (misurato il 20/09/2026 all'01:04).
#   si accende:  touch /opt/nivult/engine/logs/.fascia-silenziosa
#   si spegne:   rm    /opt/nivult/engine/logs/.fascia-silenziosa
notte() {
  [ -f /opt/nivult/engine/logs/.fascia-silenziosa ] || return 1
  local h=$(TZ=Europe/Rome date +%H%M); [ $((10#$h)) -ge 2330 ] || [ $((10#$h)) -lt 700 ]
}

while true; do
  if notte; then
    # Fermarlo a meta' lotto non perde niente: la coda si prenota con scadenza,
    # quindi le righe tornano libere da sole e nessuna viene marcata come fatta.
    mira=$(ps -eo pid,args | grep "sintesi""_mt5.py" | grep -v grep | awk '{print $1}')
    if [ -n "$mira" ]; then
      echo "$(date -u +%FT%TZ) fascia silenziosa: fermo mT5 ($mira)" >> "$LOG"
      for p in $mira; do kill "$p" 2>/dev/null; done
    fi
    sleep 300; continue
  fi

  # il freno vive anche qui, non solo dentro il demone: se la scheda e' piena
  # per colpa di qualcun altro (v1, o un container che non doveva esserci) non
  # si aggiunge benzina.
  usata=$(cat /sys/class/drm/card0/device/mem_info_gtt_used 2>/dev/null || echo 0)
  totale=$(cat /sys/class/drm/card0/device/mem_info_gtt_total 2>/dev/null || echo 1)
  if [ "$totale" -gt 0 ] && [ $(( 100 * usata / totale )) -gt 70 ]; then
    echo "$(date -u +%FT%TZ) memoria grafica al $(( 100 * usata / totale ))%, aspetto" >> "$LOG"
    sleep 120; continue
  fi
  echo "$(date -u +%FT%TZ) avvio sintesi_mt5" >> "$LOG"
  /opt/nivult/engine/.venv/bin/python /opt/nivult/sintesi_mt5.py \
    --continuo --limite 160 --lotto "${LOTTO_MT5:-8}" >> "$LOG" 2>&1
  echo "$(date -u +%FT%TZ) sintesi_mt5 uscito ($?), riparto fra 30s" >> "$LOG"
  sleep 30
done
