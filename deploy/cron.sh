#!/usr/bin/env bash
#
# I lavori periodici di Nivult, installati in modo che aggiungerne uno non
# cancelli gli altri.
#
# NASCE DA UN GUASTO VERO. Il 2026-08-28 alle 17:53 il crontab e' stato
# riscritto per installare `ats-nightly`, e `crontab <file>` SOSTITUISCE
# l'intera tabella: sono spariti in un colpo i digest orari, l'ingestione
# notturna e il backup. Nessuno se n'e' accorto perche' niente fallisce
# rumorosamente quando un cron semplicemente non c'e' piu': il mattino dopo
# l'utente non ha ricevuto il digest, la notte non era stata ingerita
# nessuna offerta, e il backup mancava da 24 ore.
#
# Questo script e' idempotente e conservativo: rimpiazza solo le proprie
# righe (riconosciute dal marcatore) e lascia intatto tutto il resto della
# tabella, comprese righe di sistema che non ci appartengono.
#
#   sudo deploy/cron.sh          installa o aggiorna
#   sudo deploy/cron.sh --check  verifica e basta, esce 1 se manca qualcosa
#
# Gli orari, e perche' sono quelli:
#   01:00  ingestione — prima dei digest, cosi' il primo giro del mattino
#          trova le offerte della notte
#   02:30  ATS/Wikidata — dopo l'ingestione, prima del backup
#   03:00  backup — a valle di tutto, con il riavvio automatico alle 04:00
#          che resta fuori dalla finestra del dump
#   :10    digest, OGNI ORA — l'orario di invio e' quello dell'utente, nel
#          suo fuso: un giro solo al giorno consegnerebbe in ritardo

set -euo pipefail

MARCATORE='# nivult'

RIGHE=$(cat <<'EOF'
0 1 * * * /opt/nivult/nightly.sh >> /var/log/nivult-nightly.log 2>&1
30 2 * * * /opt/nivult/ats-nightly.sh >> /opt/nivult/engine/logs/ats-cron.log 2>&1
0 3 * * * /opt/nivult/backup.sh >> /var/log/nivult-backup.log 2>&1
10 * * * * /opt/nivult/engine/deploy/digests.sh >> /var/log/nivult-digests.log 2>&1
EOF
)

if [[ "${1:-}" == "--check" ]]; then
  attuale=$(crontab -l 2>/dev/null || true)
  mancanti=0
  while IFS= read -r riga; do
    [[ -z "$riga" ]] && continue
    if ! grep -Fqx "$riga" <<<"$attuale"; then
      echo "MANCA: $riga"
      mancanti=$((mancanti + 1))
    fi
  done <<<"$RIGHE"
  if (( mancanti )); then
    echo "$mancanti lavori mancanti — esegui: sudo deploy/cron.sh"
    exit 1
  fi
  echo "tutti i lavori Nivult sono installati"
  exit 0
fi

attuale=$(crontab -l 2>/dev/null || true)

# Si tolgono solo le NOSTRE righe, riconosciute dal percorso: cosi' una
# riga di sistema o di un altro servizio sopravvive a questo script.
altrui=$(grep -v '/opt/nivult/' <<<"$attuale" | grep -v "^${MARCATORE}" || true)

{
  [[ -n "$altrui" ]] && printf '%s\n' "$altrui"
  printf '%s\n' "$MARCATORE"
  printf '%s\n' "$RIGHE"
} | crontab -

echo "crontab aggiornato:"
crontab -l
