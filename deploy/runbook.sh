#!/usr/bin/env bash
# IL RUNBOOK: le uniche azioni che il «medico» (Claude sul server, utente
# nivult-medico) puo' eseguire sulla produzione. Tutto il resto gli e'
# vietato per costruzione (sudoers: solo questo script). Ogni azione e'
# idempotente e stampa cosa ha fatto.
#
#   runbook.sh stato                     il quadro: demoni, sentinella, sprint, N5, backup, RAM, disco
#   runbook.sh log <nome> [righe]        coda di un log (sprint-glm, lingue, ats-cron, arricchisci-continua, ...)
#   runbook.sh riavvia <demone>          systemctl restart nivult-<demone> (solo i nostri)
#   runbook.sh sprint start|stop|status  lo sprint GLM
#   runbook.sh backup                    rispedisce il backup di oggi (SOLO_INVIO) o lo rifa'
#   runbook.sh ponte                     rilancia il ponte verso il motore
#   runbook.sh sentinella                una corsa della sentinella, adesso
#   runbook.sh sql "<SELECT ...>"        SOLA LETTURA sul db delle offerte (read-only, 30 s, 50 righe)
#   runbook.sh telegram "<testo>"        un messaggio a Giuseppe
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
DEMONI="scrape scrape-veloce profonda scoperta arricchisci volano certificati api sprint"
LOG_DIR="$BASE/logs"

case "${1:-}" in
  stato)
    echo "== $(date -u +%FT%TZ) $(hostname)"
    echo "-- demoni"; for d in $DEMONI; do printf "  %-16s %s\n" "$d" "$(systemctl is-active nivult-$d 2>/dev/null)"; done
    echo "-- sentinella"; cat /opt/nivult/sentinella-stato.json 2>/dev/null | head -c 800; echo
    echo "-- sprint"; "$BASE/deploy/sprint.sh" status 2>/dev/null | tail -2
    echo "-- backup"; cat /opt/nivult/backup-state 2>/dev/null
    echo "-- memoria/carico/disco"; free -m | sed -n 2p; uptime | sed 's/.*load/load/'; df -h / | tail -1
    echo "-- uccisioni per memoria 24h"; journalctl -k --since -24h --no-pager -q 2>/dev/null | grep -c "Out of memory"
    echo "-- battito N5 e offerte"
    "$PY" - <<'EOF' 2>/dev/null
import psycopg, re
pw = re.search(r"^POSTGRES_PASSWORD=(.*)$", open("/opt/nivult/.env").read(), re.M).group(1).strip()
c = psycopg.connect(f"postgresql://nivult:{pw}@127.0.0.1:5432/nivult_ats", autocommit=True)
print("  N5:", c.execute("SELECT nome, to_char(battito,'HH24:MI'), left(note,120) FROM operaio_battiti").fetchall())
print("  attive:", c.execute("SELECT count(*) FROM ats_jobs WHERE expired_at IS NULL").fetchone()[0],
      "| scadute 2h (viste di recente):", c.execute("SELECT count(*) FROM ats_jobs WHERE expired_at > now()-interval '2 hours' AND fetched_at > now()-interval '3 days'").fetchone()[0],
      "| sprint ultima ora:", c.execute("SELECT count(*) FROM ats_jobs WHERE sprint_at > now()-interval '1 hour'").fetchone()[0])
EOF
    ;;
  log)
    n="${2:?nome del log}"; righe="${3:-40}"
    case "$n" in */*|*..*) echo "nome non valido"; exit 2;; esac
    f="$LOG_DIR/$n.log"; [ -f "$f" ] || f="/var/log/nivult-$n.log"
    [ -f "$f" ] && tail -n "$righe" "$f" | cut -c1-300 || echo "log $n non trovato"
    ;;
  riavvia)
    d="${2:?demone}"; d="${d#nivult-}"
    case " $DEMONI " in *" $d "*) ;; *) echo "demone non ammesso: $d"; exit 2;; esac
    systemctl restart "nivult-$d"; sleep 6; echo "nivult-$d: $(systemctl is-active nivult-$d)"
    ;;
  sprint)  "$BASE/deploy/sprint.sh" "${2:-status}" "${3:-35.0}" "${4:-30}" ;;
  backup)
    oggi=$(date +%F); esiste=0; [ -s "/opt/nivult/backups/nivult-$oggi.sql.gz.enc" ] && esiste=1
    systemd-run --unit=nivult-backup-runbook --collect --quiet --setenv=SOLO_INVIO=$esiste /opt/nivult/backup.sh \
      && echo "backup avviato in background (SOLO_INVIO=$esiste); esito fra qualche minuto in: runbook.sh log backup" \
      || echo "backup: unita' gia' in corso o errore di avvio"
    ;;
  ponte)
    systemd-run --unit=nivult-ponte-runbook --collect --quiet --property=WorkingDirectory=$BASE "$PY" scripts/ponte_ats.py \
      && echo "ponte avviato in background" || echo "ponte: gia' in corso o errore"
    ;;
  sentinella) cd "$BASE" && timeout 120 "$PY" -m nivult.ats.sentinella 2>&1 | tail -3 ;;
  sql)
    q="${2:?query}"
    case "$q" in [Ss][Ee][Ll][Ee][Cc][Tt]*|[Ww][Ii][Tt][Hh]*) ;; *) echo "solo SELECT"; exit 2;; esac
    "$PY" - "$q" <<'EOF'
import sys, psycopg, re
pw = re.search(r"^POSTGRES_PASSWORD=(.*)$", open("/opt/nivult/.env").read(), re.M).group(1).strip()
c = psycopg.connect(f"postgresql://nivult:{pw}@127.0.0.1:5432/nivult_ats", autocommit=True, options="-c default_transaction_read_only=on -c statement_timeout=30000")
for r in c.execute(sys.argv[1]).fetchmany(50):
    print(r)
EOF
    ;;
  telegram)
    cd "$BASE" && "$PY" -c "
import sys; from nivult.ats import pronto_soccorso as ps
print('inviato' if ps.telegram('Medico Nivult', [], [sys.argv[1]]) else 'NON inviato')" "${2:?testo}"
    ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
