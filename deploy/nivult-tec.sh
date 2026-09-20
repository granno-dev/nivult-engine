#!/bin/bash
# Il supervisore della testa tecnologie sul N5.
#
# Un ciclo suo, non dentro nivult-n5.sh, perche' i due carichi hanno priorita'
# diverse: le tecnologie sono flusso (arrivano 84.000 annunci al giorno e vanno
# fatti), le sintesi sono inventario (si vendono, ma il magazzino non marcisce —
# il testo degli annunci scaduti si conserva all'83%). Se la scheda e' contesa,
# a cedere devono essere le sintesi.
#
# TRE SULLA STESSA SCHEDA. La Radeon 890M non ha memoria propria: la prende dalla
# RAM di sistema (46 GB di GTT). Il 16/09 due motori insieme hanno portato il
# carico a 356 e incastrato la macchina per undici ore. Per questo il freno e'
# doppio: qui prima di lanciare, e dentro il demone fra un lotto e l'altro.
set -u
cd /opt/nivult
set -a; . /opt/nivult/.env; set +a
LOG=/opt/nivult/logs/tec_v1.log
mkdir -p /opt/nivult/logs

# La fascia silenziosa, lo stesso interruttore degli altri. Le tecnologie sono
# flusso, quindi in teoria non dovrebbero fermarsi; ma il N5 sta in casa e di
# notte la ventola si sente. Restano ferme come mT5, e si recuperano di giorno:
# la capacita' e' 218.000/giorno contro 84.000 che servono, il margine c'e'.
notte() {
  [ -f /opt/nivult/engine/logs/.fascia-silenziosa ] || return 1
  local h=$(TZ=Europe/Rome date +%H%M); [ $((10#$h)) -ge 2330 ] || [ $((10#$h)) -lt 700 ]
}

while true; do
  if notte; then
    mira=$(ps -eo pid,args | grep "tec_v1""_demone.py" | grep -v grep | awk '{print $1}')
    if [ -n "$mira" ]; then
      echo "$(date -u +%FT%TZ) fascia silenziosa: fermo tec-v1 ($mira)" >> "$LOG"
      for p in $mira; do kill "$p" 2>/dev/null; done
    fi
    sleep 300; continue
  fi

  usata=$(cat /sys/class/drm/card0/device/mem_info_gtt_used 2>/dev/null || echo 0)
  totale=$(cat /sys/class/drm/card0/device/mem_info_gtt_total 2>/dev/null || echo 1)
  if [ "$totale" -gt 0 ] && [ $(( 100 * usata / totale )) -gt 70 ]; then
    echo "$(date -u +%FT%TZ) memoria grafica al $(( 100 * usata / totale ))%, aspetto" >> "$LOG"
    sleep 120; continue
  fi

  echo "$(date -u +%FT%TZ) avvio tec_v1 (${NOME_TEC:-tec-v1})" >> "$LOG"
  /opt/nivult/engine/.venv/bin/python /opt/nivult/engine/scripts/tec_v1_demone.py \
    --continuo --limite "${LIMITE_TEC:-512}" --lotto "${LOTTO_TEC:-16}" >> "$LOG" 2>&1
  echo "$(date -u +%FT%TZ) tec_v1 uscito ($?), riparto fra 30s" >> "$LOG"
  sleep 30
done
