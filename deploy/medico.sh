#!/usr/bin/env bash
# IL MEDICO: Claude Code in esecuzione sul server, svegliato dalla
# sentinella per i problemi che il pronto soccorso non sa curare.
# Gira come utente nivult-medico, che puo' fare SOLO `sudo runbook.sh`.
#
#   medico.sh "<problema 1>" ["<problema 2>" ...]
#
# Limiti (deliberati): al massimo 4 interventi l'ora (lock + contatore),
# 25 passi per intervento, strumenti consentiti elencati. Il gettone e'
# quello dell'abbonamento di Giuseppe (CLAUDE_CODE_OAUTH_TOKEN in
# /opt/nivult/.env, letto qui e mai stampato).
set -uo pipefail
BASE=/opt/nivult/engine
LOG=/var/log/nivult-medico.log
CONTA=/tmp/nivult-medico.ora
ora=$(date +%Y%m%d%H)
n=$( (grep -c "^$ora$" "$CONTA" 2>/dev/null) || echo 0)
if [ "$n" -ge 4 ]; then
  echo "$(date -Is) medico: tetto orario raggiunto (4), salto" >> "$LOG"; exit 0
fi
echo "$ora" >> "$CONTA"
export CLAUDE_CODE_OAUTH_TOKEN=$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' /opt/nivult/.env | cut -d= -f2-)
[ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] || { echo "$(date -Is) medico: gettone assente" >> "$LOG"; exit 1; }
PROBLEMI=$(printf '  - %s\n' "$@")
PROMPT="Sei il medico di Nivult, in esecuzione sul server di produzione. La sentinella ha trovato questi problemi che il pronto soccorso automatico non sa curare:
$PROBLEMI

Leggi PRIMA docs/manuale-guasti.md (le regole e le cure ammesse). Poi:
1. sudo /opt/nivult/engine/deploy/runbook.sh stato
2. i log del pezzo malato con: sudo /opt/nivult/engine/deploy/runbook.sh log <nome> 60
3. una cura ammessa dal manuale, una alla volta, poi verifica con stato
4. OBBLIGATORIO alla fine: sudo /opt/nivult/engine/deploy/runbook.sh telegram \"<resoconto in italiano, breve, numeri veri: trovato / fatto / resta>\"
Non hai altri strumenti: se serve altro, scrivilo nel resoconto. Non inventare numeri. Se non capisci la causa, dillo."
cd "$BASE"
{
  echo "=== $(date -Is) medico avviato per: $*"
  # si abbassa a nivult-medico: nessun accesso ai .env (600 root), solo il
  # runbook via sudo; il gettone passa per l'ambiente e non tocca il disco
  # Fable 5.1, lo stesso modello della chat di Giuseppe; se in modalita'
  # senza terminale non fosse disponibile, ripiego sul modello del piano
  MODELLO="${MEDICO_MODELLO:-claude-fable-5-1}"
  visita() {
    timeout 900 sudo -u nivult-medico --preserve-env=CLAUDE_CODE_OAUTH_TOKEN \
      env HOME=/home/nivult-medico claude -p "$PROMPT" $1 \
      --max-turns 25 \
      --allowedTools "Bash(sudo /opt/nivult/engine/deploy/runbook.sh *)" "Read" "Grep" "Glob" \
      --permission-mode acceptEdits 2>&1
  }
  ESITO=$(visita "--model $MODELLO"); rc=$?
  if [ $rc -ne 0 ] && echo "$ESITO" | grep -qi "model"; then
    echo "--- modello $MODELLO non disponibile qui, ripiego sul predefinito"
    ESITO=$(visita ""); rc=$?
  fi
  echo "$ESITO" | tail -60
  echo "=== $(date -Is) medico finito (rc $rc, modello $MODELLO)"
} >> "$LOG" 2>&1
