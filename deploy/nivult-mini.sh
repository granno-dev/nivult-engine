#!/bin/bash
# L'operaio del Mac mini: tiene in piedi il server del modello e il demone che lo usa.
#
# Dal 19/09/2026 il 2B non scrive piu' le sintesi: le fa mT5 sul N5. Qui resta una
# cosa sola, le TECNOLOGIE di tutto il mondo — il 2B e' l'unico dei tre che le sa
# estrarre, e senza la sintesi scrive ~32 parole invece di ~172. Per questo e'
# sparito --fuori-europa: il N5 non fa piu' il 2B, quindi il mondo passa di qui.
#
# IL RIPASSO E' SPENTO, di proposito. Serviva a riscrivere le sintesi in cui mT5 ha
# esitato, ma le sintesi alimentano il digest B2C, che oggi ha ZERO utenti; le
# tecnologie invece si vendono adesso (TheirStack le fa pagare 3 crediti). Con 8 GB
# di RAM su questo Mac le due cose non ci stanno insieme. Si riaccende con
# --ripasso il giorno del primo abbonato: la coda lo aspetta, ordinata dalla sintesi
# meno convinta in avanti, e non si perde niente nel frattempo.
#
# Lanciato da launchd a ogni accensione, e rilanciato se muore.
set -uo pipefail
cd "$HOME/nivult" || exit 1
set -a; . "$HOME/nivult/.env"; set +a
LOG="$HOME/nivult/operaio.log"
dire() { echo "$(date -Is) $*" >> "$LOG"; }

dire "=== avvio operaio del Mac mini"
while true; do
  # 1. il server del modello
  if ! pgrep -f "llama-server -m $HOME/nivult/modelli" >/dev/null; then
    dire "server del modello giu': lo avvio"
    (cd "$HOME/nivult/llama-b11026" && nohup ./llama-server \
       -m "$HOME/nivult/modelli/nivult-2b-Q4_K_M.gguf" --host 127.0.0.1 --port 8089 \
       -ngl 99 -np 8 -c 16384 -b 2048 -ub 2048 -fa on --jinja --reasoning-format none -t 4 \
       >> "$HOME/nivult/server.log" 2>&1 &)
    for i in $(seq 1 90); do curl -sf -o /dev/null http://127.0.0.1:8089/health && break; sleep 2; done
    dire "server pronto"
  fi
  # 2. il demone: solo tecnologie. La grammatica e' quella ristretta al campo
  #    `tecnologie`: quella normale pretende anche `sintesi`, e il modello
  #    sprecherebbe token a riempirla.
  if ! pgrep -f "estrai_2b.py --continuo" >/dev/null; then
    dire "demone 2b giu': lo avvio"
    nohup python3 "$HOME/nivult/estrai_2b.py" --continuo --limite 160 --par 8 \
      --solo-tecnologie \
      --grammatica "$HOME/nivult/tec.gbnf" \
      --url http://127.0.0.1:8089 >> "$HOME/nivult/estrai.log" 2>&1 &
  fi
  # 3. il battito, cosi' la sentinella su Hetzner sa che siamo vivi
  python3 - <<'PY' >> "$LOG" 2>&1 || true
import os, psycopg
with psycopg.connect(os.environ["ATS_DATABASE_URL"], connect_timeout=20, autocommit=True) as c:
    c.execute("INSERT INTO operaio_battiti (nome, battito) VALUES ('mac-mini', now()) "
              "ON CONFLICT (nome) DO UPDATE SET battito = now()")
PY
  sleep 120
done
