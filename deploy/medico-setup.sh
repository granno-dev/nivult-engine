#!/usr/bin/env bash
# Mette in piedi medico e chat Telegram con Kimi Code CLI. Idempotente.
# OAuth si fa una volta come nivult-medico con `kimi login`: nessun token nel
# repository o in /opt/nivult/.env. Sudo resta limitato al solo runbook.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
BASE=/opt/nivult/engine
UT=nivult-medico
cd "$BASE"

echo "== 1) utente e sudoers (solo il runbook)"
id "$UT" >/dev/null 2>&1 || useradd -m -s /bin/bash "$UT"
printf '# medico e chat Kimi possono eseguire SOLO il runbook\n%s ALL=(root) NOPASSWD: %s\n' \
  "$UT" "$BASE/deploy/runbook.sh" > /etc/sudoers.d/nivult-medico
chmod 440 /etc/sudoers.d/nivult-medico
visudo -c -f /etc/sudoers.d/nivult-medico >/dev/null
HOME_UT=$(getent passwd "$UT" | cut -d: -f6)

echo "== 2) permessi"
chmod +x "$BASE/deploy/runbook.sh" "$BASE/deploy/medico.sh" "$BASE/deploy/chat.sh"
chmod o+rx /opt/nivult "$BASE" 2>/dev/null || true
chmod -R o+rX "$BASE/docs" "$BASE/deploy" "$BASE/src" "$BASE/scripts" 2>/dev/null || true
touch /var/log/nivult-medico.log /var/log/nivult-chat.log
chmod 644 /var/log/nivult-medico.log /var/log/nivult-chat.log

echo "== 3) Kimi Code CLI"
if ! sudo -u "$UT" test -x "$HOME_UT/.kimi-code/bin/kimi"; then
  sudo -u "$UT" env HOME="$HOME_UT" bash -c \
    'curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash'
fi
KIMI="$HOME_UT/.kimi-code/bin/kimi"
sudo -u "$UT" env HOME="$HOME_UT" "$KIMI" --version

echo "== 4) perimetro di medico e chat"
install -d -o "$UT" -g "$UT" -m 700 "$HOME_UT/.kimi-code"
if [ ! -f "$HOME_UT/.kimi-code/config.toml" ]; then
  install -o "$UT" -g "$UT" -m 600 "$BASE/deploy/kimi-medico.toml" \
    "$HOME_UT/.kimi-code/config.toml"
else
  echo "   config esistente conservata (OAuth incluso)"
fi
sudo -u "$UT" env HOME="$HOME_UT" "$KIMI" doctor config \
  "$HOME_UT/.kimi-code/config.toml"

echo "== 5) prova OAuth e runbook"
if ! sudo -u "$UT" env HOME="$HOME_UT" timeout 120 "$KIMI" -p \
  'Rispondi solo con la parola: pronto' --output-format text; then
  echo
  echo "Autorizza Kimi una volta, poi rilancia questo setup:"
  echo "  sudo -u $UT env HOME=$HOME_UT $KIMI login"
  exit 1
fi
sudo -u "$UT" sudo "$BASE/deploy/runbook.sh" stato > /tmp/nivult-medico-runbook.txt
head -12 /tmp/nivult-medico-runbook.txt
echo "== FATTO"
