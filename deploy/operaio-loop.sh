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
battito() {   # la sentinella sul server avvisa se manca da piu' di 2 ore;
              # il cruscotto mostra la salute del N5 dalla nota (JSON)
  $PY - "$1" <<'EOF' 2>/dev/null || true
import os, sys, json, glob, psycopg
def _mem():
    m = {}
    for riga in open("/proc/meminfo"):
        k, v = riga.split(":"); m[k] = int(v.split()[0])
    return m["MemTotal"] // 1024, m["MemAvailable"] // 1024
def _temp():     # Tctl del Ryzen: sysfs e' quello dell'host anche nel container
    for f in glob.glob("/sys/class/hwmon/hwmon*/temp*_label"):
        if open(f).read().strip() in ("Tctl", "Package id 0"):
            return int(open(f.replace("_label", "_input")).read()) // 1000
    return None
tot, lib = _mem()
nota = {"fase": sys.argv[1], "ram_mb": tot, "ram_libera_mb": lib,
        "carico": round(os.getloadavg()[0], 2), "temp_c": _temp()}
with psycopg.connect(os.environ["ATS_DATABASE_URL"], autocommit=True) as c:
    c.execute("INSERT INTO operaio_battiti (nome, battito, note) VALUES ('n5', now(), %s) "
              "ON CONFLICT (nome) DO UPDATE SET battito = now(), note = EXCLUDED.note",
              (json.dumps(nota),))
EOF
}
while true; do
  echo "== giro $(date -u +%FT%TZ)"
  battito "inizio giro"
  # Il classificatore a dizionario e' sequenziale (un core) e sull'arretrato
  # trova poco (1.032 famiglie in 10 minuti il 06/09: quei casi li copre lo
  # sprint GLM). Lotti piccoli e pause lunghe: un core in boost a 77 °C
  # faceva girare la ventola del N5 «a palla» — parola di Giuseppe.
  # Il classificatore a livelli (dizionario + fuzzy sui titoli) e' il
  # passo che scalda i 16 core, e sull'arretrato rende l'1,8% a giro:
  # 300-500 classificate su 20.000 viste, sempre le stesse che non sa
  # leggere (misurato la notte del 06/09/2026, mentre GLM ne faceva
  # 28.000 l'ora). Con il file di pausa il passo si salta; si toglie
  # quando lo sprint GLM finisce o quando arriva nivult v1.
  if [ -f /opt/nivult/engine/logs/.classificatore-pausa ]; then
    echo "-- classificatore a livelli in pausa (logs/.classificatore-pausa)"
  else
    nice -n 10 $PY -m nivult.ats.classificatore_livelli --no-glm --limite 20000 2>&1 | grep -E "classificate|viste|Traceback|Error" | tail -2 || true
  fi
  nice -n 10 $PY -m nivult.ats.estrai_extra --limite 100000 2>&1 | tail -1 || true
  nice -n 10 $PY -m nivult.ats.lingue_richieste --tetto 200000 2>&1 | tail -1 || true
  nice -n 10 $PY -m nivult.ats.lingua --limite 100000 2>&1 | tail -1 || true
  # La SCOPERTA sta qui, non sul server: crawling a molti thread verso
  # migliaia di siti diversi, che sul server a 4 vCPU portava il carico a 33
  # (07/09/2026). Il ripasso del detector rilegge i domini «no_ats» con le
  # impronte nuove; la scoperta jsonld cerca sitemap + JobPosting.
  nice -n 10 $PY -m nivult.ats.detector --ripassa --limite 600 --thread 24 2>&1 | grep -E "Ripasso|Traceback" | tail -2 || true
  nice -n 10 $PY -m nivult.ats.jsonld --scopri --limite 300 --thread 16 2>&1 | tail -1 || true
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
