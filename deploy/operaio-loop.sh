#!/usr/bin/env bash
# Il ciclo dell'operaio a casa (N5): i lotti che sul server mangiavano
# RAM e CPU, qui girano in loop contro il database di Hetzner via
# Tailscale. Gira DENTRO il container nivult-operaio; lo avvia
# /root/operaio-loop-start.sh sul N5. Log in engine/logs/operaio.log.
#
# Cosa fa (e cosa NON fa: lo scraping resta sul server, con l'IP del
# datacenter; i digest e l'API pure):
#   - classificatore a livelli senza GLM (dizionario + codici): 60k/giro
#   - estrai_extra (contratto + contatto dal testo): 100k/giro
#   - lingue richieste sulle nuove
#   - lingua del testo sulle nuove
set -uo pipefail
set -a; . /opt/nivult/.env; set +a
cd /opt/nivult/engine
PY=.venv/bin/python
battito() {   # la sentinella sul server avvisa se manca da piu' di 2 ore
  $PY - "$1" <<'EOF' 2>/dev/null || true
import os, sys, psycopg
with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
    c.execute("INSERT INTO operaio_battiti (nome, battito, note) VALUES ('n5', now(), %s) "
              "ON CONFLICT (nome) DO UPDATE SET battito = now(), note = EXCLUDED.note", (sys.argv[1],))
EOF
}
while true; do
  echo "== giro $(date -u +%FT%TZ)"
  battito "inizio giro"
  # Il classificatore a dizionario e' sequenziale (un core) e sull'arretrato
  # trova poco (1.032 famiglie in 10 minuti il 06/09: quei casi li copre lo
  # sprint GLM). Lotti piccoli e pause lunghe: un core in boost a 77 °C
  # faceva girare la ventola del N5 «a palla» — parola di Giuseppe.
  nice -n 10 $PY -m nivult.ats.classificatore_livelli --no-glm --limite 20000 2>&1 | grep -E "classificate|viste|Traceback|Error" | tail -2 || true
  nice -n 10 $PY -m nivult.ats.estrai_extra --limite 100000 2>&1 | tail -1 || true
  nice -n 10 $PY -m nivult.ats.lingue_richieste --tetto 200000 2>&1 | tail -1 || true
  nice -n 10 $PY -m nivult.ats.lingua --limite 100000 2>&1 | tail -1 || true
  # Render detector (Chrome headless) una volta al giorno: sul server era il
  # piu' goloso di RAM (8 processi uccisi dal kernel il 05/09); qui ha 48 GB
  # e un IP residenziale che i career site bloccano meno.
  TIMBRO=/opt/nivult/engine/logs/.render-detector.timbro
  if [ ! -f "$TIMBRO" ] || [ $(( $(date +%s) - $(stat -c %Y "$TIMBRO") )) -gt 82800 ]; then
    echo "-- render detector (60 grandi)"
    nice -n 10 $PY -m nivult.ats.detector --render --limite 60 --dip-minimi 3000 --thread 2 2>&1 | tail -2 || true
    touch "$TIMBRO"
  fi
  battito "fine giro"
  sleep 900
done
