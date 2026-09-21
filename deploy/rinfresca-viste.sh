#!/bin/bash
# Rinfresca le viste materializzate del magazzino (una volta al giorno, dopo i
# dettagli). Sta in un file con /opt/nivult/ nel percorso perche' cron.sh
# riconosce i suoi lavori da quello: una riga senza quel pezzo viene
# conservata come «sconosciuta» E reinstallata, cioe' duplicata (21/09).
set -u
docker exec -i nivult-db-1 psql -U nivult -d nivult_ats -v ON_ERROR_STOP=1 \
  -c "SET statement_timeout = '30min'" \
  -c "REFRESH MATERIALIZED VIEW CONCURRENTLY azienda_tecnologie" \
  && echo "$(date -u +%FT%TZ) azienda_tecnologie rinfrescata"
