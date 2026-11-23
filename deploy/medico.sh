#!/usr/bin/env bash
# IL MEDICO: Kimi Code CLI in esecuzione sul server, svegliato dalla
# sentinella per i problemi che il pronto soccorso non sa curare.
# Gira come utente nivult-medico, che puo' fare SOLO `sudo runbook.sh`.
#
#   medico.sh "<problema 1>" ["<problema 2>" ...]
#
# Limiti (deliberati): al massimo 4 interventi l'ora, 25 passi per
# intervento, strumenti consentiti in deploy/kimi-medico.toml. Le credenziali
# OAuth stanno nella home di nivult-medico e non passano per il repository.
set -uo pipefail
BASE=/opt/nivult/engine
LOG=/var/log/nivult-medico.log
CONTA=/tmp/nivult-medico.ora
ora=$(date +%Y%m%d%H)
n=$(grep -c "^$ora$" "$CONTA" 2>/dev/null || true)
if [ "$n" -ge 4 ]; then
  echo "$(date -Is) medico: tetto orario raggiunto (4), salto" >> "$LOG"; exit 0
fi
echo "$ora" >> "$CONTA"
KIMI=/home/nivult-medico/.kimi-code/bin/kimi
[ -x "$KIMI" ] || { echo "$(date -Is) medico: Kimi CLI non installato" >> "$LOG"; exit 1; }
PROBLEMI=$(printf '  - %s\n' "$@")
PROMPT="Sei il medico di Nivult, in esecuzione sul server di produzione. La sentinella ha trovato questi problemi che il pronto soccorso automatico non sa curare:
$PROBLEMI

Leggi PRIMA docs/manuale-guasti.md (regole e cure ammesse) e /home/nivult-medico/chat/appunti.md (storia). Gli appunti vecchi sono solo contesto: stato, numeri e incidenti vanno sempre verificati live. Poi:
1. sudo /opt/nivult/engine/deploy/runbook.sh stato
2. i log del pezzo malato con: sudo /opt/nivult/engine/deploy/runbook.sh log <nome> 60
3. una cura ammessa dal manuale, una alla volta, poi verifica con stato
4. OBBLIGATORIO alla fine: sudo /opt/nivult/engine/deploy/runbook.sh telegram \"<resoconto in italiano, breve, numeri veri: trovato / fatto / resta>\"
Non hai altri strumenti: se serve altro, scrivilo nel resoconto. Non inventare numeri o cure. Se un guasto non e' nel manuale, diagnostica e segnala; aggiorna gli appunti con data, causa verificata, azione ed esito."
cd "$BASE"
INIZIO=$(date -Is)
{
  echo "=== $INIZIO medico avviato per: $*"
  # Si abbassa a nivult-medico: nessun accesso ai .env (600 root), solo il
  # runbook via sudo. OAuth resta nella home dell'utente; gli strumenti e il
  # tetto di 25 passi sono imposti da deploy/kimi-medico.toml.
  ESITO=$(timeout 900 sudo -u nivult-medico \
    env HOME=/home/nivult-medico "$KIMI" -p "$PROMPT" \
    --output-format text < /dev/null 2>&1); rc=$?
  echo "$ESITO" | tail -60
  # il resoconto DEVE arrivare: se nel registro del runbook non c'e' un
  # «telegram» dopo l'inizio della visita, lo manda lo script con la
  # risposta del medico (che a volte dice «inviato» senza averlo fatto)
  if ! awk -v t="$INIZIO" '$1 >= t && / telegram /' /var/log/nivult-runbook.log 2>/dev/null | grep -q .; then
    echo "--- il medico non ha scritto su Telegram: lo faccio io con la sua risposta"
    cd "$BASE" && "$BASE/.venv/bin/python" - "$ESITO" <<'EOF'
import sys
from nivult.ats import pronto_soccorso as ps
testo = sys.argv[1].strip()[-1800:] or "(nessuna risposta dal medico)"
print("inviato" if ps.telegram("Medico Nivult (resoconto ripreso dal log)", [], [testo]) else "NON inviato")
EOF
  fi
  # il diario: la visita resta in medico_visite, per la revisione settimanale
  T=$(mktemp); printf %s "$ESITO" | tail -c 8000 > "$T"
  cd "$BASE" && "$BASE/.venv/bin/python" -m nivult.ats.diario registra visita "$*" "$T" \
    $(( $(date +%s) - $(date -d "$INIZIO" +%s) )) 2>&1
  rm -f "$T"
  echo "=== $(date -Is) medico finito (rc $rc, motore Kimi)"
} >> "$LOG" 2>&1
