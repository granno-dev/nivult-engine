#!/usr/bin/env bash
# Lo spazzino del disco (09/10/2026): la causa numero uno dei guasti del
# mattino e' lo spazio che finisce su file dimenticati — export morti a
# meta', duckdb.tmp di relay falliti, backup che si accumulano. Gira ogni
# notte PRIMA del backup (02:45): cancella il pattume e registra quanto
# resta libero. Le ritenzioni vere restano ai loro padroni (mantieni-export
# ruota gli snapshot, backup.sh ruota i dump locali): qui si cancella solo cio'
# che nessun padrone reclama.
set -uo pipefail
LIBERATI=0
pesa() { du -sb "$1" 2>/dev/null | cut -f1 || echo 0; }

rimuovi_se_stale() {
  local f="$1"
  [ -f "$f" ] || return 0
  [ "$(find "$f" -type f -mtime +1 2>/dev/null)" ] || return 0
  LIBERATI=$((LIBERATI + $(pesa "$f")))
  rm -f "$f" && echo "spazzino: tolto $f"
}

# 1. export morti a meta' (.tmp piu' vecchi di un giorno)
for f in /opt/nivult/exports/*.tmp; do
  rimuovi_se_stale "$f"
done

# 2. Solo file DuckDB stale: le directory temporanee e un .duckdb.tmp giovane
# possono appartenere al builder attivo e non vanno mai rimossi.
for f in /mnt/HC_Volume_106941692/*.duckdb.tmp; do
  rimuovi_se_stale "$f"
done
for d in /mnt/HC_Volume_106941692/duckdb-tmp /mnt/HC_Volume_106941692/tmp-duckdb; do
  [ -d "$d" ] || continue
  while IFS= read -r f; do
    rimuovi_se_stale "$f"
  done < <(find "$d" -type f -mtime +1 2>/dev/null)
done

# 3. backup sul volume: mai piu' dei 2 piu' recenti (la staging ora e' su
#    root; questi sono gli storici — l'off-site sull'N5 li ha comunque)
ls -t /mnt/HC_Volume_106941692/backups/nivult-*.sql.gz.enc 2>/dev/null | tail -n +3 | while read -r f; do
  LIBERATI=$((LIBERATI + $(pesa "$f")))
  rm -f "$f" && echo "spazzino: tolto $f"
done

echo "spazzino: liberati $(( LIBERATI / 1073741824 )) GB — ora liberi: root $(df --output=avail -BG / | tail -1), volume $(df --output=avail -BG /mnt/HC_Volume_106941692 | tail -1)"
