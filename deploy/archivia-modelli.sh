#!/bin/bash
# Sposta sul N5 i MODELLI freddi. Lo script notturno muove solo i file di dati
# (*.jsonl.gz, *.gguf, *.pt) e lascia indietro le cartelle dei modelli: il 18/09/2026
# il disco di Hetzner e' arrivato al 93% proprio per quelle.
# Ogni cartella si cancella solo dopo aver verificato che la copia sul N5 ha lo stesso
# numero di file e la stessa dimensione.
set -uo pipefail
K=/root/.ssh/id_ed25519_n5; N5=root@100.119.200.7
SSH="ssh -n -o BatchMode=yes -o StrictHostKeyChecking=no -o ConnectTimeout=30 -i $K"
BASE=/mnt/user/nivult-archivio/modelli
$SSH $N5 "mkdir -p $BASE" || { echo "N5 non raggiungibile, non tocco niente"; exit 1; }

for d in "$@"; do
  [ -d "$d" ] || { echo "  salto $d (non esiste)"; continue; }
  nome=$(basename "$d")
  n_loc=$(find "$d" -type f | wc -l | tr -d ' ')
  b_loc=$(du -sb "$d" | cut -f1)
  echo "  $nome: $n_loc file, $((b_loc/1048576)) MB -> copio"
  rsync -a -e "ssh -o BatchMode=yes -o StrictHostKeyChecking=no -i $K" "$d/" "$N5:$BASE/$nome/" || { echo "    copia FALLITA, lascio stare"; continue; }
  n_rem=$($SSH $N5 "find $BASE/$nome -type f | wc -l" | tr -d ' ')
  b_rem=$($SSH $N5 "du -sb $BASE/$nome | cut -f1")
  if [ "$n_loc" = "$n_rem" ] && [ "$b_loc" = "$b_rem" ]; then
    rm -rf "$d"; echo "    verificato ($n_rem file, stessa dimensione) -> rimosso da Hetzner"
  else
    echo "    DIVERSO (locale $n_loc/$b_loc, remoto $n_rem/$b_rem): NON cancello"
  fi
done
echo "disco adesso: $(df -h / | awk 'NR==2{print $4 " liberi (" $5 " usato)"}')"
