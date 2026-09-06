#!/usr/bin/env bash
# Mette in piedi IL MEDICO sul server: Claude Code in esecuzione locale,
# svegliato dalla sentinella per i guasti che il pronto soccorso non sa
# curare. Da lanciare come root SUL SERVER (o dal Mac via ssh). Idempotente.
#
#   1. Node + Claude Code (npm)
#   2. utente nivult-medico, senza privilegi, che puo' eseguire SOLO
#      `sudo /opt/nivult/engine/deploy/runbook.sh` (sudoers)
#   3. permessi: il repo leggibile, i .env NO (restano 600 root)
#   4. prova: il gettone dell'abbonamento risponde (CLAUDE_CODE_OAUTH_TOKEN in /opt/nivult/.env)
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
BASE=/opt/nivult/engine

echo "== 1) Node e Claude Code"
if ! command -v node >/dev/null; then apt-get install -y -qq nodejs npm >/tmp/medico-apt.log 2>&1; fi
echo "   node $(node -v) npm $(npm -v)"
if ! command -v claude >/dev/null; then npm install -g @anthropic-ai/claude-code >/tmp/medico-npm.log 2>&1; fi
echo "   claude $(claude --version 2>&1 | head -1)"

echo "== 2) utente nivult-medico e sudoers (solo il runbook)"
id nivult-medico >/dev/null 2>&1 || useradd -m -s /bin/bash nivult-medico
printf '# il medico (Claude sul server) puo'"'"' eseguire SOLO il runbook\nnivult-medico ALL=(root) NOPASSWD: /opt/nivult/engine/deploy/runbook.sh\n' > /etc/sudoers.d/nivult-medico
chmod 440 /etc/sudoers.d/nivult-medico
visudo -c -f /etc/sudoers.d/nivult-medico >/dev/null && echo "   sudoers ok"

echo "== 3) permessi"
chmod +x "$BASE/deploy/runbook.sh" "$BASE/deploy/medico.sh"
chmod o+rx /opt/nivult "$BASE" 2>/dev/null || true
chmod -R o+rX "$BASE/docs" "$BASE/deploy" "$BASE/src" "$BASE/scripts" 2>/dev/null || true
ls -l /opt/nivult/.env "$BASE/.env" | awk '{print "   " $1, $3, $9}'
touch /var/log/nivult-medico.log; chmod 644 /var/log/nivult-medico.log

echo "== 4) prova del gettone (una domanda da un passo)"
TOK=$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' /opt/nivult/.env | cut -d= -f2-)
[ -n "$TOK" ] || { echo "   gettone assente in /opt/nivult/.env"; exit 1; }
cd "$BASE"
sudo -u nivult-medico --preserve-env=CLAUDE_CODE_OAUTH_TOKEN env CLAUDE_CODE_OAUTH_TOKEN="$TOK" HOME=/home/nivult-medico \
  claude -p "Rispondi solo con la parola: pronto" --max-turns 1 2>&1 | tail -2
echo "== 5) prova del runbook come nivult-medico"
sudo -u nivult-medico sudo /opt/nivult/engine/deploy/runbook.sh stato | head -12
echo "== FATTO"
