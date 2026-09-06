#!/bin/bash
# IL GUARDIANO: gira sul N5 (Unraid, cron ogni 10 minuti) e controlla il
# server da FUORI. La sentinella sul server non puo' accorgersi di se'
# stessa: se il server e' giu', o il cron e' fermo, o Postgres non
# risponde, e' questo script che lo dice a Giuseppe.
#
# Tre controlli: l'API risponde (HTTPS pubblico); il database risponde
# via Tailscale; la sentinella ha battuto negli ultimi 20 minuti.
# Avvisa dopo DUE fallimenti di fila (20 min), non al primo, e manda un
# solo «rientrato» quando torna. Stato in /tmp (si azzera al riavvio).
#
# Nel .env dell'operaio servono TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID.
ENV=/mnt/cache/appdata/nivult-operaio/.env
STATO=/tmp/nivult-guardiano
LOG=/mnt/cache/appdata/nivult-operaio/guardiano.log
tok=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV" | cut -d= -f2-)
chat=$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV" | cut -d= -f2-)
dsn=$(grep -E '^ATS_DATABASE_URL=' "$ENV" | cut -d= -f2-)

telegram() {
  [ -n "$tok" ] && [ -n "$chat" ] || { echo "$(date -Is) niente token/chat: $1" >> "$LOG"; return; }
  curl -s -m 20 -X POST "https://api.telegram.org/bot$tok/sendMessage" \
    --data-urlencode "chat_id=$chat" --data-urlencode "text=$1" > /dev/null
}

guasti=""
# 1. l'API pubblica
code=$(curl -s -m 20 -o /dev/null -w '%{http_code}' https://api.nivult.com/cruscotto)
[ "$code" = 200 ] || [ "$code" = 401 ] || [ "$code" = 403 ] || guasti="$guasti\n• API: HTTP ${code:-timeout}"
# 2. il database via Tailscale, e 3. il battito della sentinella (dal container, che ha psycopg)
eta=$(docker exec -e DSN="$dsn" nivult-operaio /opt/nivult/engine/.venv/bin/python -c '
import os, psycopg
with psycopg.connect(os.environ["DSN"], connect_timeout=15) as c:
    r = c.execute("SELECT extract(epoch FROM now()-battito)::int FROM operaio_battiti WHERE nome=%s", ("sentinella",)).fetchone()
    print(r[0] if r else -1)' 2>&1 | tail -1)
case "$eta" in
  ''|*[!0-9-]*) guasti="$guasti\n• database non raggiungibile da qui: ${eta:0:80}" ;;
  -1) guasti="$guasti\n• la sentinella non ha mai battuto" ;;
  *) [ "$eta" -le 1200 ] || guasti="$guasti\n• sentinella ferma da $(( eta / 60 )) minuti" ;;
esac

n=$(cat "$STATO.fallimenti" 2>/dev/null || echo 0)
if [ -n "$guasti" ]; then
  n=$((n+1)); echo "$n" > "$STATO.fallimenti"
  echo "$(date -Is) fallimento $n:$(printf "$guasti" | tr '\n' ' ')" >> "$LOG"
  if [ "$n" -eq 2 ]; then
    telegram "$(printf '🔴 Guardiano N5: il server non risponde bene da 20 minuti%b\n(controllato da casa, via Tailscale e HTTPS)' "$guasti")"
    touch "$STATO.avvisato"
  fi
else
  if [ -f "$STATO.avvisato" ]; then
    telegram "🟢 Guardiano N5: il server risponde di nuovo (API, database, sentinella)"
    rm -f "$STATO.avvisato"
  fi
  echo 0 > "$STATO.fallimenti"
fi
