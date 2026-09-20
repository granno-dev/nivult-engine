#!/bin/bash
# La testa tecnologie sul Mac mini M2. Prende il posto del 2B, spento il 20/09.
#
# PERCHE' QUI E NON SUL N5. Misurato il 20/09 sullo stesso modello e sullo stesso
# tipo di annunci:
#   Mac mini M2 (10 core, memoria unificata)   388.000 annunci/giorno
#   N5 Radeon 890M, da sola                    218.000
#   N5 Radeon 890M, con v1 e mT5 accanto       125.000
# L'M2 vince perche' ha la sua banda di memoria; la 890M la divide con la CPU ed
# era contesa da tre modelli. Spostando qui la testa, il N5 torna a due carichi —
# la configurazione che funzionava.
#
# Il 2B che stava qui e' spento: stessa macchina, stesso ruolo, modello migliore
# (coda lunga 50,1% contro 30,1%, che e' il numero che il prodotto vende).
#
# IL BATTITO. La sentinella su Hetzner dichiara «operaio mac-mini muto» dopo due
# ore senza una riga in operaio_battiti. Il vecchio supervisore del 2B batteva a
# ogni giro; questo lanciava il demone in primo piano e non tornava mai al giro,
# quindi dal 20/09 alle 09:51 il Mac mini risultava muto pur macinando 385.000
# offerte al giorno — e il medico stava «curando» un guasto che non c'era
# (incidente 129). Ora il demone gira in sottofondo e il giro batte ogni 2 minuti.
set -u
cd "$HOME/nivult" || exit 1
set -a; . "$HOME/nivult/.env"; set +a
export MODELLO_TEC="$HOME/nivult/modelli/tec-v1"
export NOME_TEC="${NOME_TEC:-tec-v1-ck00500}"
LOG="$HOME/nivult/tec_v1.log"
# 8 GB di RAM su questa macchina: un mmBERT ne vuole ~2,5 e ce ne stanno. La
# soglia e' bassa di proposito (0,4 GB): e' un freno d'emergenza. Una soglia
# alta si autoblocca, perche' la memoria che manca la occupa il modello stesso.
libera_gb() {
  vm_stat | awk '/Pages free/{f=$3} /Pages inactive/{i=$3} END{printf "%.1f", (f+i)*16384/1073741824}'
}
batti() {
  "$HOME/nivult/venv-tec/bin/python" - <<'PY' >> "$LOG" 2>&1 || true
import os, psycopg
with psycopg.connect(os.environ["ATS_DATABASE_URL"], connect_timeout=20, autocommit=True) as c:
    c.execute("INSERT INTO operaio_battiti (nome, battito) VALUES ('mac-mini', now()) "
              "ON CONFLICT (nome) DO UPDATE SET battito = now()")
PY
}
while true; do
  if ! pgrep -f "tec_v1_demone.py" >/dev/null; then
    g=$(libera_gb)
    if awk "BEGIN{exit !($g < 0.4)}"; then
      echo "$(date -Is) memoria libera $g GB, aspetto" >> "$LOG"
    else
      echo "$(date -Is) avvio tec-v1 ($NOME_TEC), memoria libera $g GB" >> "$LOG"
      nohup "$HOME/nivult/venv-tec/bin/python" "$HOME/nivult/tec_v1_demone.py" \
        --continuo --limite "${LIMITE_TEC:-512}" --lotto "${LOTTO_TEC:-8}" >> "$LOG" 2>&1 &
    fi
  fi
  batti
  sleep 120
done
