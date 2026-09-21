#!/usr/bin/env bash
#
# I lavori periodici di Nivult, installati in modo che aggiungerne uno non
# cancelli gli altri.
#
# NASCE DA UN GUASTO VERO. Il 2026-08-28 alle 17:53 il crontab e' stato
# riscritto per installare `ats-nightly`, e `crontab <file>` SOSTITUISCE
# l'intera tabella: sono spariti in un colpo i digest orari, l'ingestione
# notturna e il backup. Nessuno se n'e' accorto perche' niente fallisce
# rumorosamente quando un cron semplicemente non c'e' piu': il mattino dopo
# l'utente non ha ricevuto il digest, la notte non era stata ingerita
# nessuna offerta, e il backup mancava da 24 ore.
#
# Questo script e' idempotente e conservativo: rimpiazza solo le proprie
# righe (riconosciute dal marcatore) e lascia intatto tutto il resto della
# tabella, comprese righe di sistema che non ci appartengono.
#
#   sudo deploy/cron.sh          installa o aggiorna
#   sudo deploy/cron.sh --check  verifica e basta, esce 1 se manca qualcosa
#
# Gli orari, e perche' sono quelli:
#   01:00  ingestione — prima dei digest, cosi' il primo giro del mattino
#          trova le offerte della notte
#   02:30  ATS/Wikidata — dopo l'ingestione, prima del backup
#   03:00  backup — a valle di tutto, con il riavvio automatico alle 04:00
#          che resta fuori dalla finestra del dump
#   05:00  ponte ATS — dopo che l'ATS delle 02:30 ha scaricato e
#          classificato, e dopo il riavvio delle 04:00 che troncherebbe un
#          travaso a meta'. Prima del primo digest utile delle 07:10:
#          un'offerta travasata alle 05:00 puo' finire nel digest di quella
#          stessa mattina
#   04:30  retention dei dati personali — dopo il backup delle 03:00, cosi'
#          se un termine e' sbagliato i dati sono ancora nel dump della
#          notte, e prima del ponte delle 05:00
#   23:00  jsonld --scopri — le aziende SENZA ATS che pubblicano il
#          JobPosting sul proprio sito (70.760 mai provate al 13/09/2026,
#          il modulo esisteva ma nessuno lo pianificava). Alle 23 perche'
#          dalle 10 all'01 la tabella e' vuota, e' fuori dal riavvio delle
#          04:00 che troncherebbe un'ora di rete, e i domini promossi
#          entrano in ats_companies in tempo per il giro ATS delle 02:30.
#          1.500 domini = ~60 min misurati (50 in 120 s, 4 thread);
#          timeout 5400 perche' una chiamata appesa non arrivi all'01:00.
#   22:15  radar Indeed — i DATORI che assumono questa settimana, dal link
#          «candidati sul sito dell'azienda»: solo il dominio, mai il
#          contenuto (decisione di Giuseppe, 13/09/2026). 300 ricerche a
#          rotazione su 16 paesi (~12 min); i domini entrano in coda al
#          detector con precedenza, i tenant ATS in ats_companies. Prima
#          del jsonld delle 23 e dell'ATS delle 02:30, che li raccolgono.
#          Gira da Hetzner: mai dall'IP di casa (regola del 13/09/2026).
#   21:30  bilanci ESEF — le quotate UE/SEE da filings.xbrl.org (7.357
#          entita', ~400 bilanci a notte, ~18 min): ricavi, utile, attivo,
#          patrimonio in `bilanci`, e il LEI agganciato ai nostri domini
#          dal namespace della tassonomia (14/15 misurati il 13/09/2026).
#          Quando l'arretrato e' finito, ogni notte prende solo i depositi
#          nuovi. Prima del radar delle 22:15.
#   :10    digest, OGNI ORA — l'orario di invio e' quello dell'utente, nel
#          suo fuso: un giro solo al giorno consegnerebbe in ritardo
#
# ⚠ E' SUCCESSO DI NUOVO, il 06/09/2026 alle 19:36. Questo script toglieva
# ogni riga contenente `/opt/nivult/` e reinstallava solo quelle che
# conosceva: una lista incompleta ha quindi CANCELLATO 16 lavori veri —
# la sentinella ogni 5 minuti, i passi diurni, gli export, le pulizie —
# e ha degradato il ponte da ogni 30 minuti a una volta al giorno. Niente
# ha fatto rumore: un cron che non c'e' piu' non fallisce, tace.
#
# Due difese, da non togliere:
#   1. la lista qui sotto e' COMPLETA — chi aggiunge un lavoro a mano lo
#      aggiunge anche qui, altrimenti il prossimo giro se lo porta via;
#   2. le righe Nivult che questo script NON conosce vengono CONSERVATE e
#      segnalate, non cancellate. Meglio un doppione visibile che un
#      lavoro sparito in silenzio.
# In piu' la tabella precedente viene salvata in /opt/nivult/crontab-*.txt.

set -euo pipefail

MARCATORE='# nivult'

RIGHE=$(cat <<'EOF'
0 1 * * * /opt/nivult/nightly.sh >> /var/log/nivult-nightly.log 2>&1
30 2 * * * /opt/nivult/ats-nightly.sh >> /opt/nivult/engine/logs/ats-cron.log 2>&1
0 3 * * * /opt/nivult/backup.sh >> /var/log/nivult-backup.log 2>&1
*/30 * * * * /opt/nivult/engine/deploy/ponte-ats.sh >> /var/log/nivult-ponte-ats.log 2>&1
30 4 * * * /opt/nivult/engine/deploy/retention-utenti.sh >> /var/log/nivult-retention.log 2>&1
10 * * * * /opt/nivult/engine/deploy/digests.sh >> /var/log/nivult-digests.log 2>&1
*/5 * * * * cd /opt/nivult/engine && .venv/bin/python -m nivult.ats.sentinella >> /var/log/nivult-sentinella.log 2>&1
0 0,6,12,18 * * * /opt/nivult/engine/deploy/salute.sh >> /var/log/nivult-salute.log 2>&1
20 * * * * find /opt/nivult/exports/flusso -name "novita-*.jsonl.gz" -mmin +2880 -delete
15 6 * * * find /opt/nivult/exports -name "*.jsonl.gz" -mtime +7 -delete
30 5 * * * cd /opt/nivult/engine && PW=$(grep -E "^POSTGRES_PASSWORD=" /opt/nivult/.env | head -1 | cut -d= -f2-) ATS_DATABASE_URL="postgresql://nivult:${PW}@127.0.0.1:5432/nivult_ats" .venv/bin/python -m nivult.ats.wikidata_ditte --limite 1500 >> /var/log/nivult-esporta.log 2>&1
35 5 * * * cd /opt/nivult/engine && PW=$(grep -E "^POSTGRES_PASSWORD=" /opt/nivult/.env | head -1 | cut -d= -f2-) ATS_DATABASE_URL="postgresql://nivult:${PW}@127.0.0.1:5432/nivult_ats" .venv/bin/python -m nivult.ats.benchmark_salari >> /var/log/nivult-esporta.log 2>&1
40 5 * * * cd /opt/nivult/engine && PW=$(grep -E "^POSTGRES_PASSWORD=" /opt/nivult/.env | head -1 | cut -d= -f2-) ATS_DATABASE_URL="postgresql://nivult:${PW}@127.0.0.1:5432/nivult_ats" .venv/bin/python -m nivult.ats.segnali --aggiorna --segnali >> /var/log/nivult-esporta.log 2>&1
45 5 * * * cd /opt/nivult/engine && PW=$(grep -E "^POSTGRES_PASSWORD=" /opt/nivult/.env | head -1 | cut -d= -f2-) ATS_DATABASE_URL="postgresql://nivult:${PW}@127.0.0.1:5432/nivult_ats" .venv/bin/python -m nivult.ats.esporta --attive --aziende --scadute --giorni 7 >> /var/log/nivult-esporta.log 2>&1
30 4 * * * /opt/nivult/engine/deploy/passo-diurno.sh estrai-extra nivult.ats.estrai_extra --limite 200000
0 5 * * * /opt/nivult/engine/deploy/passo-diurno.sh salari-testo nivult.ats.salari --testo --limite 600000
45 5 * * * /opt/nivult/engine/deploy/passo-diurno.sh registri nivult.ats.registri_imprese --limite 3000
50 5 * * * /opt/nivult/engine/deploy/passo-diurno.sh registri nivult.ats.registri_imprese --mix
30 6 * * * /opt/nivult/engine/deploy/passo-diurno.sh domini nivult.ats.domini_datori --limite 2000
0 7 * * * /opt/nivult/engine/deploy/passo-diurno.sh dettagli nivult.ats.dettagli --limite 400000
40 7 * * * /opt/nivult/engine/deploy/passo-diurno.sh aziende-dettagli nivult.ats.aziende_dettagli --limite 20000
20 7 * * * docker exec -i nivult-db-1 psql -U nivult -d nivult_ats -c "REFRESH MATERIALIZED VIEW CONCURRENTLY azienda_tecnologie" >> /var/log/nivult-esporta.log 2>&1
30 7 * * * /opt/nivult/engine/deploy/passo-diurno.sh scheda-sito nivult.ats.scheda_sito --limite 400
15 8 * * * /opt/nivult/engine/deploy/passo-diurno.sh loghi-dominio nivult.ats.loghi --da-dominio --limite 2500
0 9 * * * /opt/nivult/engine/deploy/passo-diurno.sh glm-extra nivult.ats.estrai_extra --glm 600
0 10 * * * /opt/nivult/engine/deploy/passo-diurno.sh organico nivult.ats.organico_dichiarato
30 7 * * 1 /opt/nivult/engine/deploy/revisione-settimanale.sh >> /var/log/nivult-chat.log 2>&1
25 * * * * cd /opt/nivult/engine && timeout 1500 .venv/bin/python -m nivult.ats.canarini --controlla >> /var/log/nivult-canarini.log 2>&1
40 6 * * 1 cd /opt/nivult/engine && .venv/bin/python -m nivult.ats.canarini --scegli >> /var/log/nivult-canarini.log 2>&1
0 23 * * * cd /opt/nivult/engine && PW=$(grep -E "^POSTGRES_PASSWORD=" /opt/nivult/.env | head -1 | cut -d= -f2-) ATS_DATABASE_URL="postgresql://nivult:${PW}@127.0.0.1:5432/nivult_ats" timeout 5400 .venv/bin/python -m nivult.ats.jsonld --scopri --limite 1500 --thread 4 >> /var/log/nivult-jsonld.log 2>&1
15 22 * * * cd /opt/nivult/engine && PW=$(grep -E "^POSTGRES_PASSWORD=" /opt/nivult/.env | head -1 | cut -d= -f2-) ATS_DATABASE_URL="postgresql://nivult:${PW}@127.0.0.1:5432/nivult_ats" timeout 1800 .venv/bin/python -m nivult.ats.radar_indeed --giro --limite 300 >> /var/log/nivult-radar-indeed.log 2>&1
30 21 * * * cd /opt/nivult/engine && PW=$(grep -E "^POSTGRES_PASSWORD=" /opt/nivult/.env | head -1 | cut -d= -f2-) ATS_DATABASE_URL="postgresql://nivult:${PW}@127.0.0.1:5432/nivult_ats" timeout 3600 .venv/bin/python -m nivult.ats.bilanci --esef --limite 400 >> /var/log/nivult-bilanci.log 2>&1
EOF
)

if [[ "${1:-}" == "--check" ]]; then
  attuale=$(crontab -l 2>/dev/null || true)
  mancanti=0
  while IFS= read -r riga; do
    [[ -z "$riga" ]] && continue
    if ! grep -Fqx "$riga" <<<"$attuale"; then
      echo "MANCA: $riga"
      mancanti=$((mancanti + 1))
    fi
  done <<<"$RIGHE"
  if (( mancanti )); then
    echo "$mancanti lavori mancanti — esegui: sudo deploy/cron.sh"
    exit 1
  fi
  echo "tutti i lavori Nivult sono installati"
  exit 0
fi

attuale=$(crontab -l 2>/dev/null || true)

# La rete di sicurezza: la tabella di prima si salva prima di toccarla.
if [[ -n "$attuale" ]]; then
  copia="/opt/nivult/crontab-$(date +%Y%m%d-%H%M%S).txt"
  printf '%s\n' "$attuale" > "$copia" 2>/dev/null && echo "tabella precedente salvata in $copia"
fi

# Le righe che non sono nostre passano intatte. Si toglie pero'
# l'intestazione che questo script stesso scrive sopra le righe ignote,
# o si accumulerebbe una copia a ogni esecuzione.
altrui=$(grep -v '/opt/nivult/' <<<"$attuale" | grep -v "^${MARCATORE}" \
         | grep -v '^# righe Nivult non elencate' || true)

# Righe superate, da togliere se ci sono ancora: l'unico modo ammesso di
# cancellare qualcosa e' nominarlo qui, per esteso, dove si vede nel
# diff. Il ponte a 05:00 e' stato sostituito da quello ogni 30 minuti.
OBSOLETE=$(cat <<'EOF'
0 5 * * * /opt/nivult/engine/deploy/ponte-ats.sh >> /var/log/nivult-ponte-ats.log 2>&1
EOF
)

# Le righe NOSTRE che questo script non conosce si CONSERVANO e si
# segnalano. E' la differenza fra «lo script e' la fonte di verita'» e
# «lo script cancella cio' che non ha ancora imparato»: la seconda ha
# gia' spento la sentinella una volta.
ignote=$(grep '/opt/nivult/' <<<"$attuale" | grep -v "^#" | while IFS= read -r r; do
  [[ -z "$r" ]] && continue
  grep -Fqx "$r" <<<"$RIGHE" && continue
  grep -Fqx "$r" <<<"$OBSOLETE" && continue
  printf '%s\n' "$r"
done)

{
  [[ -n "$altrui" ]] && printf '%s\n' "$altrui"
  printf '%s\n' "$MARCATORE"
  printf '%s\n' "$RIGHE"
  if [[ -n "$ignote" ]]; then
    printf '%s\n' "# righe Nivult non elencate in deploy/cron.sh — conservate, da riportare nello script"
    printf '%s\n' "$ignote"
  fi
} | crontab -

if [[ -n "$ignote" ]]; then
  echo "⚠ conservate $(grep -c . <<<"$ignote") righe Nivult che cron.sh non conosce:"
  printf '   %s\n' "$ignote"
  echo "   riportale in RIGHE, dentro deploy/cron.sh, o al prossimo giro resteranno doppie."
fi

echo "crontab aggiornato:"
crontab -l
