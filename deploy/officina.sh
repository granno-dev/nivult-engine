#!/usr/bin/env bash
# L'OFFICINA: un adapter e' rotto (i canarini tacciono, o il ripiego trova
# in pagina offerte che l'adapter non vede) e Claude sul server lo ripara.
#
#   officina.sh <piattaforma> [--slug <tenant>] [--senza-deploy] [--motivo "<testo>"]
#
# 1. dossier: campione della pagina (sanificato), attese, la classe, le regole
# 2. il clone dell'officina (/home/nivult-medico/officina/repo) allineato a main
# 3. Claude (utente nivult-medico) modifica SOLO adapters.py nel suo clone e
#    prova sul banco finche' non passa
# 4. verifica a macchina: perimetro del diff + banco di prova + lettura viva
# 5. deploy: commit, push al bare (checkout automatico), riavvio scraper,
#    canarini col codice nuovo; se tacciono, rollback
# 6. officina_riparazioni, diario, Telegram — sempre
#
# Limiti: una riparazione per piattaforma ogni 6 ore, sei al giorno in tutto.
set -uo pipefail
BASE=/opt/nivult/engine
PY="$BASE/.venv/bin/python"
OFF=/home/nivult-medico/officina
REPO=$OFF/repo
BARE=/opt/nivult/engine.git
LOG=/var/log/nivult-officina.log
UT=nivult-medico
PID="${1:?piattaforma}"; shift
SLUG=""; CAMP=""; DEPLOY=1; MOTIVO="adapter $PID: i canarini o il ripiego dicono che non legge piu' la bacheca"
while [ $# -gt 0 ]; do case "$1" in
  --slug) SLUG="$2"; shift 2 ;; --campione) CAMP="$2"; shift 2 ;; --senza-deploy) DEPLOY=0; shift ;; --motivo) MOTIVO="$2"; shift 2 ;; *) shift ;; esac; done
INIZIO=$(date +%s)
log() { echo "$(date -Is) [$PID] $*" >> "$LOG"; }
telegram() { cd "$BASE" && "$PY" -c "
import sys; from nivult.ats import pronto_soccorso as ps
ps.telegram('Officina Nivult', [], [sys.argv[1]])" "$1" >/dev/null 2>&1 || true; }
diario() { T=$(mktemp); printf %s "$2" > "$T"; cd "$BASE" && "$PY" -m nivult.ats.diario registra officina "$1" "$T" $(( $(date +%s) - INIZIO )) >> "$LOG" 2>&1; rm -f "$T"; }

# --- limiti di frequenza --------------------------------------------------
ULT=/tmp/nivult-officina-$PID.ultima; CONTA=/tmp/nivult-officina.giorno; oggi=$(date +%Y%m%d)
if [ -f "$ULT" ] && [ $(( $(date +%s) - $(cat "$ULT") )) -lt 21600 ]; then log "gia' aperta meno di 6h fa: salto"; exit 0; fi
if [ "$( (grep -c "^$oggi$" "$CONTA" 2>/dev/null) || echo 0)" -ge 6 ]; then log "sei officine oggi: basta"; telegram "🔧 Officina: sesta richiesta di oggi ($PID), mi fermo. Serve una mano umana."; exit 0; fi
date +%s > "$ULT"; echo "$oggi" >> "$CONTA"
export CLAUDE_CODE_OAUTH_TOKEN=$(grep -E '^CLAUDE_CODE_OAUTH_TOKEN=' /opt/nivult/.env | cut -d= -f2-)
[ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] || { log "gettone Claude assente"; telegram "🔧 Officina su $PID: il gettone di Claude manca (scade settembre 2027)."; exit 1; }
log "=== apertura: $MOTIVO"

# --- 1. il dossier ---------------------------------------------------------
if ! OUT=$(cd "$BASE" && "$PY" -m nivult.ats.officina dossier "$PID" ${SLUG:+--slug "$SLUG"} ${CAMP:+--campione "$CAMP"} 2>&1); then
  log "dossier fallito: $OUT"; telegram "🔧 Officina su $PID: non riesco a preparare il dossier — ${OUT: -300}"; exit 1; fi
log "$OUT"
D=$OFF/$PID

# --- 2. il clone dell'officina -------------------------------------------
if [ ! -d "$REPO/.git" ]; then
  install -d -o $UT -g $UT -m 750 "$OFF"
  git clone -q "$BARE" "$REPO" && chown -R $UT:$UT "$REPO"
fi
sudo -u $UT git -C "$REPO" fetch -q origin && sudo -u $UT git -C "$REPO" reset -q --hard origin/main && sudo -u $UT git -C "$REPO" clean -fdq
install -o $UT -g $UT -m 644 "$BASE/deploy/officina-CLAUDE.md" "$D/CLAUDE.md"
# il BANCO come comando fisso: la regola di permesso di Claude e' un
# prefisso letterale del comando, e un percorso lungo con opzioni in
# ordine libero non lo si azzecca mai. `./banco` e `./banco --vivo` si'.
cat > "$D/banco" <<EOF
#!/usr/bin/env bash
exec $PY $REPO/scripts/prova_adapter.py $PID --repo $REPO --campione $D/campione.html --attese $D/attese.json "\$@"
EOF
chown $UT:$UT "$D/banco"; chmod 755 "$D/banco"
sed -i "s|{{PID}}|$PID|g; s|{{REPO}}|$REPO|g; s|{{PY}}|$PY|g; s|{{BANCO}}|$D/banco|g" "$D/CLAUDE.md"

# --- 3. Claude ripara nel suo clone ---------------------------------------
# Regole di permesso: i percorsi assoluti si scrivono con «//» (con una
# barra sola sono relativi alla cartella di lavoro e la regola non
# scatta mai: e' cosi' che la prima esercitazione e' finita senza
# poter scrivere). --add-dir apre il clone come cartella di lavoro.
PROMPT="Sei in officina. L'adapter «$PID» non legge piu' la bacheca. Leggi DOSSIER.md in questa cartella e ripara la classe nel clone $REPO/src/nivult/ats/adapters.py, provando sul banco con ./banco finche' non passa (PROVA: OK, e una volta anche ./banco --vivo). Non fare commit. Chiudi con due righe: cosa hai cambiato e perche'."
MODELLO="${MEDICO_MODELLO:-claude-fable-5-1}"
ripara() {
  cd "$D" && timeout 1500 sudo -u $UT --preserve-env=CLAUDE_CODE_OAUTH_TOKEN env HOME=/home/$UT \
    claude -p "$PROMPT" $1 --max-turns 40 --add-dir "$REPO" \
    --allowedTools "Read" "Grep" "Glob" "Edit(//$REPO/src/nivult/ats/adapters.py)" "Bash(./banco *)" "Bash(./banco)" "Bash($D/banco *)" "Bash($D/banco)" \
    --permission-mode acceptEdits < /dev/null 2>&1
}
ESITO=$(ripara "--model $MODELLO"); rc=$?
if [ $rc -ne 0 ] && echo "$ESITO" | grep -qi "model"; then ESITO=$(ripara ""); rc=$?; fi
log "claude rc=$rc: $(printf %s "$ESITO" | tail -c 600 | tr '\n' ' ')"

# --- 4. la verifica a macchina --------------------------------------------
VER=$(cd "$BASE" && "$PY" -m nivult.ats.officina verifica "$PID" 2>&1); vrc=$?
log "verifica rc=$vrc: $(printf %s "$VER" | tr '\n' ' ' | cut -c1-600)"
if [ $vrc -eq 3 ]; then
  # Non c'era niente da riparare: il canarino aveva visto un vuoto
  # passeggero del portale (taleez, 08/09/2026: tre canarini a zero alle
  # 20:38, tutti pieni alle 21:36). Si chiude in pace, senza chiamare
  # nessuno: un allarme che grida al lupo insegna a ignorare gli allarmi.
  sudo -u $UT git -C "$REPO" checkout -q -- . 2>/dev/null
  telegram "🔧 Officina su $PID: FALSO ALLARME, l'adapter legge.
$(printf %s "$VER" | tail -4 | cut -c1-160)
Niente da riparare, niente deployato."
  diario "$MOTIVO" "FALSO ALLARME (l'adapter legge, nessuna modifica)
--- verifica:
$VER"
  exit 0
fi
if [ $vrc -ne 0 ]; then
  cd "$BASE" && "$PY" -m nivult.ats.officina bocciata "$PID" "$MOTIVO" "$VER" >/dev/null 2>&1
  sudo -u $UT git -C "$REPO" checkout -q -- . 2>/dev/null
  telegram "🔧 Officina su $PID: riparazione BOCCIATA dalla verifica, niente deployato.
$(printf %s "$VER" | tail -6 | cut -c1-160)
Il dossier resta in $D. Serve una mano umana."
  diario "$MOTIVO" "BOCCIATA
--- claude:
$(printf %s "$ESITO" | tail -c 2500)
--- verifica:
$VER"
  exit 1
fi

# --- 5. il deploy, o no ----------------------------------------------------
if [ $DEPLOY -eq 0 ]; then
  log "senza deploy (prova): il diff resta nel clone"
  telegram "🔧 Officina su $PID (PROVA, senza deploy): la riparazione passa il banco.
$(printf %s "$VER" | tail -3 | cut -c1-160)"
  diario "$MOTIVO (prova senza deploy)" "OK SUL BANCO
$(printf %s "$ESITO" | tail -c 2000)
--- verifica:
$VER"
  exit 0
fi
DEP=$(cd "$BASE" && "$PY" -m nivult.ats.officina deploy "$PID" "$MOTIVO" 2>&1); drc=$?
log "deploy rc=$drc: $DEP"
SPIEGA=$(printf %s "$ESITO" | tail -c 500)
if [ $drc -eq 0 ]; then
  telegram "🟢 Officina su $PID: adapter riparato e DEPLOYATO.
$DEP
Claude: $SPIEGA"
else
  telegram "🟠 Officina su $PID: deployato e poi ROLLBACK, i canarini tacevano ancora.
$DEP
Il dossier resta in $D. Serve una mano umana."
fi
diario "$MOTIVO" "$( [ $drc -eq 0 ] && echo DEPLOYATA || echo ROLLBACK )
$DEP
--- claude:
$(printf %s "$ESITO" | tail -c 2500)
--- verifica:
$VER"
log "=== chiusura in $(( $(date +%s) - INIZIO )) s"
exit $drc
