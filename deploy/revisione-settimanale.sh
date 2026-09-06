#!/usr/bin/env bash
# LA REVISIONE SETTIMANALE: il lunedì alle 07:30 UTC Claude sul server
# rilegge il diario della settimana (visite del medico, chat, incidenti)
# e scrive a Giuseppe su Telegram cosa ha imparato: quali guasti sono
# tornati, quali cure il pronto soccorso dovrebbe imparare, quali righe
# aggiungere al manuale. Passa dalla chat (deploy/chat.sh), cosi' la
# risposta ha memoria e finisce nel diario.
set -uo pipefail
BASE=/opt/nivult/engine
CHAT=$(cd "$BASE" && "$BASE/.venv/bin/python" -c "from nivult.ats import pronto_soccorso as ps; print(ps._chat_id() or '')")
[ -n "$CHAT" ] || { echo "nessun chat id"; exit 1; }
F=$(mktemp -p /var/lib/nivult-chat msg-XXXXXX.txt 2>/dev/null || mktemp)
cat > "$F" <<'EOF'
Revisione settimanale del diario. Lancia `sudo /opt/nivult/engine/deploy/runbook.sh diario settimana` e leggi il risultato. Poi rispondi in massimo 25 righe: (1) i guasti che si sono ripetuti e perché; (2) le cure che il medico ha dovuto trovare da solo e che il pronto soccorso potrebbe imparare (con il comando esatto del runbook usato); (3) le righe da aggiungere a docs/manuale-guasti.md, scritte pronte da incollare; (4) cosa NON è chiaro e va guardato da Giuseppe. Se la settimana è stata vuota, dillo in due righe. Aggiorna anche appunti.md con le conclusioni.
EOF
"$BASE/deploy/chat.sh" "$CHAT" "$F"
