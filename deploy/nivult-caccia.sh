#!/bin/bash
#
# ⚠ SUPERATO — NON USARE. Il cacciatore gira su Hetzner dalla sera del
# 19/09/2026: lo script vivo e' `deploy/nivult-caccia-hetzner.sh`, e sul server
# ne esiste una copia chiamata `/opt/nivult/nivult-caccia.sh` (stesso contenuto,
# nome diverso: e' quella che viene lanciata). Le motivazioni qui sotto NON
# valgono piu': HomeShield del router bloccava le verifiche e falsificava
# l'archivio (4.557 aziende rimesse in coda a mano), e mT5, con cui il
# cacciatore doveva convivere, e' spento dal 21/09. Si conserva solo per
# riaccendere il cacciatore in casa se un giorno servisse.
#
# Il cacciatore di domini, in produzione dal 19/09/2026.
#
# Perche' qui e non su Hetzner: non usa la scheda grafica — fa richieste HTTP e
# interroga SearXNG — quindi convive con mT5 senza togliergli niente. E SearXNG
# gira su questa macchina: dall'IP di casa i motori rispondono, da un IP di
# datacenter molto meno.
#
# Perche' conta: al 19/09 conoscevamo il dominio di 4.886 aziende su 65.417 con
# offerte attive (7,5%). Senza dominio non si aggancia niente — ne' settore, ne'
# dipendenti, ne' bilanci — e il mercato compra l'AZIENDA, non l'annuncio:
# TheirStack fa pagare un credito per ogni azienda rivelata.
#
# Resa misurata sulla coda vera il 19/09: 24% su 120 aziende, 581 aziende l'ora.
set -u
cd /opt/nivult
set -a; . /opt/nivult/.env; set +a
LOG=/opt/nivult/logs/caccia_domini.log
mkdir -p /opt/nivult/logs

while true; do
  # SearXNG e' una delle tre fonti: se e' giu' la caccia rende molto meno, ma le
  # altre due (logo nella bacheca, email negli annunci) funzionano lo stesso.
  # Quindi si parte comunque, e lo si annota.
  if ! curl -s -m 8 -o /dev/null "http://127.0.0.1:8888/"; then
    echo "$(date -u +%FT%TZ) attenzione: searxng non risponde, resto sulle altre due fonti" >> "$LOG"
  fi
  echo "$(date -u +%FT%TZ) avvio caccia_domini" >> "$LOG"
  /opt/nivult/engine/.venv/bin/python /opt/nivult/caccia_domini.py \
    --continuo --limite 120 --par "${PAR_CACCIA:-6}" >> "$LOG" 2>&1
  echo "$(date -u +%FT%TZ) caccia_domini uscito ($?), riparto fra 60s" >> "$LOG"
  sleep 60
done
