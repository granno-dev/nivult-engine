#!/bin/bash
# Rinfresca le viste materializzate del magazzino (una volta al giorno, dopo i
# dettagli). Sta in un file con /opt/nivult/ nel percorso perche' cron.sh
# riconosce i suoi lavori da quello: una riga senza quel pezzo viene
# conservata come «sconosciuta» E reinstallata, cioe' duplicata (21/09).
#
# 11/10/2026: ASPETTA l'export di oggi invece di correre sopra — l'export
# ormai dura ore e il refresh sotto export andava in timeout (misurato:
# la copertura cruscotto oltre i 30 minuti). Attesa massima 3 ore, poi
# rinfresca comunque: la vista di ieri e' meglio di niente, ma quella di
# stamattina e' meglio ancora.
set -u

MANIFEST=/opt/nivult/exports/manifest-ultimo.json
oggi=$(date -u +%F)
for i in $(seq 1 360); do
  [ -f "$MANIFEST" ] && grep -q "$oggi" "$MANIFEST" && break
  sleep 30
done

docker exec -i nivult-db-1 psql -U nivult -d nivult_ats -v ON_ERROR_STOP=1 \
  -c "SET statement_timeout = '30min'" \
  -c "REFRESH MATERIALIZED VIEW CONCURRENTLY azienda_tecnologie" \
  && echo "$(date -u +%FT%TZ) azienda_tecnologie rinfrescata"

# le viste del cruscotto (11/10/2026): i conteggi che sotto carico superano
# i timeout della pagina si misurano qui, una volta al giorno
docker exec -i nivult-db-1 psql -U nivult -d nivult_ats -v ON_ERROR_STOP=1 \
  -c "SET statement_timeout = '60min'" \
  -c "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_cruscotto_copertura" \
  -c "REFRESH MATERIALIZED VIEW CONCURRENTLY mv_cruscotto_non_class" \
  && echo "$(date -u +%FT%TZ) viste cruscotto rinfrescate"
