#!/usr/bin/env bash
# Attiva il guardrail HR senza mostrare il contenuto del file ambiente.
set -euo pipefail
ENV_FILE="${1:?uso: attiva-family-guard.sh /percorso/.env}"
test -f "$ENV_FILE"
tmp=$(mktemp "$ENV_FILE.family-guard.XXXXXX")
trap 'rm -f "$tmp"' EXIT
awk '
  BEGIN { trovata = 0 }
  /^FAMILY_GUARD=/ {
    if (!trovata) print "FAMILY_GUARD=1"
    trovata = 1
    next
  }
  { print }
  END { if (!trovata) print "FAMILY_GUARD=1" }
' "$ENV_FILE" > "$tmp"
chmod 600 "$tmp"
mv "$tmp" "$ENV_FILE"
printf 'FAMILY_GUARD attivo in %s\n' "$(dirname "$ENV_FILE")"
