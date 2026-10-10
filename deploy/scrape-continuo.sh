#!/usr/bin/env bash
# Lo scrape che non dorme: a lotti di 500, in continuo, dando priorita'
# alle aziende mai viste (le 47mila del tesoro) e poi alle attive piu'
# stantie — cosi' le vive restano fresche come col polling di Fantastic.
# Gira come servizio: quando la coda delle mai-viste e' vuota, continua
# a rinfrescare le attive, sempre a partire da chi ne ha piu' bisogno.
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
POSTGRES_PASSWORD=$(grep -E '^POSTGRES_PASSWORD=' /opt/nivult/.env | head -1 | cut -d= -f2-)
export ATS_DATABASE_URL="postgresql://nivult:${POSTGRES_PASSWORD}@127.0.0.1:5432/nivult_ats"
cd "$BASE"
# 09/10/2026: l'auto-guarigione. Durante lo spostamento di pgdata il db e'
# sparito per minuti e i runner sono restati APPESI su connessioni morte —
# il servizio risultava «active» ma non leggeva piu' nessun tenant, e il
# Restart=always di systemd non serve se il processo non muore. Due reti:
#  1. pg_isready a inizio giro: se il db non risponde per 3 volte, esci —
#     systemd ci riavvia, e a quel punto il db e' tornato;
#  2. timeout sul lotto: un lotto da 500 non puo' durare 45 minuti; se
#     succede, e' un incaglio — si uccide e il giro ricomincia pulito.
fallimenti_db=0
while true; do
  if ! docker exec nivult-db-1 pg_isready -U nivult -d nivult_ats -q; then
    fallimenti_db=$((fallimenti_db + 1))
    if [ "$fallimenti_db" -ge 3 ]; then
      echo "scrape: db irraggiungibile per 3 giri, esco — systemd riavvia" >&2
      exit 1
    fi
    sleep 30
    continue
  fi
  fallimenti_db=0
  # 26/09/2026: il grep mostrava SOLO le righe «scrape:» — un traceback
  # del runner spariva nella pipe e il loop riprovava cieco per sempre.
  # Ora passano anche errori e traceback.
  timeout -k 30 45m "$PY" -m nivult.ats.runner --limite 500 --thread 16 2>&1 \
    | grep -E "scrape:|Traceback|Error|error" || true
  # una pausa breve tra i lotti: gentilezza verso le piattaforme
  # condivise (greenhouse, lever...) per non farsi limitare
  sleep 12
done
