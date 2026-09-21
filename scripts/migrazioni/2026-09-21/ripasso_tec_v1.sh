#!/bin/bash
# Le 406.826 offerte attive lette dalla testa tec v1 (ck00500) tornano in coda alla
# v2: lotti da 2.000 in transazioni brevi, con tentativi contro i deadlock.
# La coda della testa e' `tec_v1_at IS NULL` per posted_at DESC: sono le piu'
# recenti, quindi passano davanti alle 2,3M mai lette — costa circa un giorno
# di Mac mini (406k/giorno). Sul golden v2 (lotto 1) la v1 aveva scritto 3
# nomi dove ce n'erano 31.
set -u
LOTTO=${LOTTO:-2000}
PSQL="docker exec -i nivult-db-1 psql -U nivult -d nivult_ats -At -v ON_ERROR_STOP=1"
while :; do
  SQL="BEGIN; CREATE TEMP TABLE lotto AS
         SELECT t.job_id FROM tecnologie_v1 t JOIN ats_jobs j ON j.id = t.job_id
          WHERE t.modello = 'tec-v1-ck00500' AND j.expired_at IS NULL AND j.tec_v1_at IS NOT NULL LIMIT $LOTTO;
       UPDATE ats_jobs j SET tec_v1_at = NULL, preso_tec_at = NULL FROM lotto l WHERE j.id = l.job_id;
       SELECT count(*) FROM lotto; COMMIT;"
  n=""
  for t in 1 2 3 4 5 6 7 8 9 10; do
    n=$($PSQL -c "$SQL" 2>/tmp/ripasso-tec-err.log | grep -E '^[0-9]+$' | tail -1) && [ -n "$n" ] && break
    echo "$(date -u +%T) lotto fallito (tentativo $t): $(tail -1 /tmp/ripasso-tec-err.log)"; sleep $((5 * t))
  done
  [ -z "$n" ] && { echo "mi fermo: dieci tentativi falliti"; exit 1; }
  echo "$(date -u +%T) rimesse in coda $n"
  [ "$n" -lt "$LOTTO" ] && break
done
echo FINE-RIPASSO-TEC
