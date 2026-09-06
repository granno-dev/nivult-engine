#!/usr/bin/env bash
# LA CHAT: Giuseppe scrive al bot Telegram, l'API consegna il messaggio qui
# (deploy/chat.sh <chat_id> <file-col-testo>), e risponde Claude in
# esecuzione sul server — lo stesso «medico», con memoria della
# conversazione (`claude --continue` nella cartella /home/nivult-medico/chat)
# e gli stessi limiti: solo il runbook via sudo.
#
# Comandi secchi, senza Claude:  /stato   /silenzio 30m   /silenzio 0
#                                /medico <testo>   /diario   /nuova
# Tutto il resto e' una domanda per Claude.
set -uo pipefail
BASE=/opt/nivult/engine
LOG=/var/log/nivult-chat.log
CASA=/home/nivult-medico/chat
CHAT_ID="${1:?chat id}"; FILE="${2:?file}"
TESTO=$(cat "$FILE" 2>/dev/null); rm -f "$FILE"
[ -n "$TESTO" ] || exit 0

rispondi() {  # testo -> Telegram (spezzato a 3900)
  cd "$BASE" && "$BASE/.venv/bin/python" - "$CHAT_ID" "$1" <<'EOF'
import sys
from nivult.ats import pronto_soccorso as ps
from nivult.delivery.telegram import invia_testo
ps._pronto_bot()  # carica TELEGRAM_BOT_TOKEN dal .env (non e' nell'ambiente di systemd-run)
chat, t = sys.argv[1], sys.argv[2].strip() or "(risposta vuota)"
while t:
    invia_testo(chat, t[:3900]); t = t[3900:]
EOF
}
runbook() { "$BASE/deploy/runbook.sh" "$@" 2>&1; }

echo "$(date -Is) da $CHAT_ID: $(printf %s "$TESTO" | head -c 200 | tr '\n' ' ')" >> "$LOG"
case "$TESTO" in
  /stato*)   rispondi "$(runbook stato | head -c 3800)"; exit 0 ;;
  /diario*)  rispondi "$(runbook diario 8 | head -c 3800)"; exit 0 ;;
  /silenzio*)
    m=$(printf %s "$TESTO" | grep -oE '[0-9]+' | head -1); u=$(printf %s "$TESTO" | grep -oE '[0-9]+ *[hm]' | grep -oE '[hm]$' || true)
    [ "${u:-m}" = h ] && m=$(( ${m:-1} * 60 ))
    rispondi "$(runbook silenzio "${m:-30}")"; exit 0 ;;
  /medico*)
    motivo=$(printf %s "$TESTO" | cut -c8- | sed 's/^ *//'); [ -n "$motivo" ] || motivo="controllo chiesto da Giuseppe via Telegram"
    rispondi "Chiamo il medico: «$motivo». Il resoconto arriva qui."
    "$BASE/deploy/medico.sh" "$motivo"; exit 0 ;;
  /nuova*)   touch "$CASA/.nuova"; rispondi "Conversazione nuova: parto senza memoria di quella precedente (gli appunti restano)."; exit 0 ;;
  /*)        rispondi "Comandi: /stato /diario /silenzio 30m /medico <testo> /nuova — oppure scrivimi una domanda."; exit 0 ;;
esac

# --- domanda libera: Claude ---------------------------------------------
# tetto: 20 risposte l'ora (e' l'abbonamento di Giuseppe, non un pozzo)
CONTA=/tmp/nivult-chat.ora; ora=$(date +%Y%m%d%H)
if [ "$( (grep -c "^$ora$" "$CONTA" 2>/dev/null) || echo 0)" -ge 20 ]; then
  rispondi "Ho risposto 20 volte in quest'ora: riprendo alla prossima."; exit 0
fi
echo "$ora" >> "$CONTA"
export CLAUDE_CODE_OAUTH_TOKEN=$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' /opt/nivult/.env | cut -d= -f2-)
[ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] || { rispondi "Il gettone di Claude sul server manca (o e' scaduto: settembre 2027)."; exit 1; }

# la casa della chat: istruzioni dal repo (si aggiornano col deploy), appunti persistenti
install -d -o nivult-medico -g nivult-medico -m 700 "$CASA"
install -o nivult-medico -g nivult-medico -m 644 "$BASE/deploy/chat-CLAUDE.md" "$CASA/CLAUDE.md"
[ -f "$CASA/appunti.md" ] || install -o nivult-medico -g nivult-medico -m 644 /dev/null "$CASA/appunti.md"
CONTINUA="--continue"; [ -f "$CASA/.nuova" ] && { rm -f "$CASA/.nuova"; CONTINUA=""; }

INIZIO=$(date +%s)
(
  # un messaggio alla volta: due domande in fila si accodano, non si intrecciano
  flock -w 600 9 || exit 1
  MODELLO="${MEDICO_MODELLO:-claude-fable-5-1}"
  PROMPT="Messaggio di Giuseppe via Telegram: $TESTO"
  ESITO=$(cd "$CASA" && timeout 600 sudo -u nivult-medico --preserve-env=CLAUDE_CODE_OAUTH_TOKEN \
      env HOME=/home/nivult-medico claude -p "$PROMPT" $CONTINUA --model "$MODELLO" \
      --max-turns 20 \
      --allowedTools "Bash(sudo /opt/nivult/engine/deploy/runbook.sh *)" "Read" "Grep" "Glob" "Edit(/home/nivult-medico/chat/appunti.md)" \
      --permission-mode acceptEdits < /dev/null 2>&1); rc=$?
  if [ $rc -ne 0 ] && echo "$ESITO" | grep -qi "model"; then
    ESITO=$(cd "$CASA" && timeout 600 sudo -u nivult-medico --preserve-env=CLAUDE_CODE_OAUTH_TOKEN \
      env HOME=/home/nivult-medico claude -p "$PROMPT" $CONTINUA --max-turns 20 \
      --allowedTools "Bash(sudo /opt/nivult/engine/deploy/runbook.sh *)" "Read" "Grep" "Glob" "Edit(/home/nivult-medico/chat/appunti.md)" \
      --permission-mode acceptEdits < /dev/null 2>&1); rc=$?
  fi
  [ -n "$ESITO" ] || ESITO="(nessuna risposta, rc $rc)"
  rispondi "$ESITO"
  echo "$(date -Is) risposta rc=$rc in $(( $(date +%s) - INIZIO )) s: $(printf %s "$ESITO" | head -c 300 | tr '\n' ' ')" >> "$LOG"
  T=$(mktemp); printf %s "$ESITO" > "$T"
  cd "$BASE" && "$BASE/.venv/bin/python" -m nivult.ats.diario registra chat "$TESTO" "$T" $(( $(date +%s) - INIZIO )) >> "$LOG" 2>&1
  rm -f "$T"
) 9>/tmp/nivult-chat.lock
