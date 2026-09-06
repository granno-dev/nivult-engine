#!/usr/bin/env bash
# Pubblica Postgres ANCHE sull'indirizzo Tailscale del server, cosi'
# l'operaio a casa (il N5 su Unraid, deciso da Giuseppe il 2026-09-06)
# puo' lavorare sul database senza che il database esca su internet:
# 100.117.204.17 esiste solo dentro la rete Tailscale. 127.0.0.1 resta.
#
# Idempotente. Riavvia il container del database (pochi secondi): i loop
# riprendono da soli, lo sprint GLM va rilanciato a mano.
set -euo pipefail
IP_TS=$(tailscale ip -4)
[ -n "$IP_TS" ] || { echo "tailscale non attivo"; exit 1; }
cd /opt/nivult
if grep -q "$IP_TS:5432:5432" docker-compose.yml; then
  echo "compose gia' aggiornato ($IP_TS)"
else
  cp docker-compose.yml "docker-compose.yml.bak-$(date +%F-%H%M)"
  python3 - "$IP_TS" <<'EOF'
import sys
ip = sys.argv[1]
p = "/opt/nivult/docker-compose.yml"
s = open(p).read()
vecchio = '      - "127.0.0.1:5432:5432"\n'
nuovo = vecchio + f'      # l\'operaio a casa (N5, Unraid) entra SOLO dalla rete Tailscale\n      - "{ip}:5432:5432"\n'
assert vecchio in s, "riga della porta non trovata"
open(p, "w").write(s.replace(vecchio, nuovo, 1))
print("compose aggiornato")
EOF
fi
docker compose up -d
sleep 6
docker ps --format '{{.Names}} {{.Status}}' | grep db
ss -ltn | grep 5432
