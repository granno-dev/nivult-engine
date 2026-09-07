#!/usr/bin/env bash
# Lo sprint GLM come unita' systemd TEMPORANEA: si ferma e riparte per
# nome, non per pattern. Il 2026-09-06 un `pkill -f` ha ucciso tre volte
# la shell che lo lanciava, perche' la riga di lancio conteneva il nome
# dello script. Con systemd-run quel modo di sbagliare non esiste.
#
#   deploy/sprint.sh start [TETTO_SPESA] [PARALLELE]   avvia (default 35 dollari, 60 chiamate parallele)
#   deploy/sprint.sh stop                  ferma pulito (riprendibile)
#   deploy/sprint.sh status                stato e ultime righe del log
set -uo pipefail
UNIT=nivult-sprint
BASE=/opt/nivult/engine
case "${1:-status}" in
  start)
    if systemctl is-active --quiet "$UNIT"; then echo "gia' in esecuzione"; exit 0; fi
    PW=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
    GLM=$(grep -E '^GLM_API_KEY=' "$BASE/.env" | cut -d= -f2-)
    systemd-run --unit="$UNIT" --collect --quiet \
      --property=WorkingDirectory="$BASE" \
      --property=OOMScoreAdjust=500 \
      --property=StandardOutput=append:"$BASE/logs/sprint-glm.log" \
      --property=StandardError=append:"$BASE/logs/sprint-glm.log" \
      --setenv=ATS_DATABASE_URL="postgresql://nivult:${PW}@127.0.0.1:5432/nivult_ats" \
      --setenv=GLM_API_KEY="$GLM" \
      --setenv=TETTO_SPESA="${2:-35.0}" \
      --setenv=SPRINT_PAR="${3:-30}" \
      "$BASE/.venv/bin/python" /opt/nivult/sprint_glm.py
    sleep 2; systemctl is-active "$UNIT" && echo "sprint avviato (unita' $UNIT)";;
  stop)
    systemctl stop "$UNIT" 2>/dev/null && echo "sprint fermato" || echo "non era in esecuzione";;
  status)
    systemctl is-active "$UNIT" || true
    systemctl show -p MainPID -p ActiveEnterTimestamp "$UNIT" 2>/dev/null | tr '\n' ' '; echo
    tail -3 "$BASE/logs/sprint-glm.log";;
  *) echo "uso: $0 start [tetto] | stop | status"; exit 2;;
esac
