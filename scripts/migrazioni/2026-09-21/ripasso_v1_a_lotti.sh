#!/bin/bash
# Il ripasso di v1 dal solo titolo, A LOTTI: l'UPDATE unico su 793.000 righe e'
# morto di deadlock contro i demoni che timbrano ats_jobs (21/09, 11:50).
# Lotti da 20.000 in transazioni brevi, con tre tentativi per lotto.
#
#   bash ripasso_v1_a_lotti.sh 1   # passo 1: azzera seniority/contratto/remoto/dichiarati_at
#   python -m nivult.ats.dichiarati --limite 1000000   # passo 2, da /opt/nivult/engine
#   bash ripasso_v1_a_lotti.sh 3   # passo 3: via le famiglie di v1, locale_v1_at a NULL
set -u
PASSO=${1:?passo 1 o 3}
# 2.000 e non 20.000: al passo 3, con v1 che gia' timbra le righe rimesse in
# coda, i lotti da 20.000 morivano di deadlock tre volte di fila (13:27).
LOTTO=${LOTTO:-2000}
PSQL="docker exec -i nivult-db-1 psql -U nivult -d nivult_ats -At -v ON_ERROR_STOP=1"
$PSQL -c "ALTER TABLE ripasso_v1_dal_titolo ADD COLUMN IF NOT EXISTS fase int NOT NULL DEFAULT 0" >/dev/null
while :; do
  if [ "$PASSO" = 1 ]; then
    SQL="BEGIN; CREATE TEMP TABLE lotto AS SELECT job_id FROM ripasso_v1_dal_titolo WHERE fase < 1 LIMIT $LOTTO;
         UPDATE ats_jobs j SET seniority = NULL, employment_type = NULL, remote = NULL, dichiarati_at = NULL FROM lotto l WHERE j.id = l.job_id;
         UPDATE ripasso_v1_dal_titolo r SET fase = 1 FROM lotto l WHERE r.job_id = l.job_id;
         SELECT count(*) FROM lotto; COMMIT;"
  else
    SQL="BEGIN; CREATE TEMP TABLE lotto AS SELECT job_id FROM ripasso_v1_dal_titolo WHERE fase < 3 LIMIT $LOTTO;
         DELETE FROM job_classifications x USING lotto l WHERE x.job_id = l.job_id AND x.model = 'nivult-v1';
         UPDATE ats_jobs j SET locale_v1_at = NULL FROM lotto l WHERE j.id = l.job_id;
         UPDATE ripasso_v1_dal_titolo r SET fase = 3 FROM lotto l WHERE r.job_id = l.job_id;
         SELECT count(*) FROM lotto; COMMIT;"
  fi
  n=""
  for t in 1 2 3 4 5 6 7 8 9 10; do
    n=$($PSQL -c "$SQL" 2>/tmp/ripasso-err.log | grep -E '^[0-9]+$' | tail -1) && [ -n "$n" ] && break
    echo "$(date -u +%T) lotto fallito (tentativo $t): $(tail -1 /tmp/ripasso-err.log)"; sleep $((5 * t))
  done
  [ -z "$n" ] && { echo "mi fermo: dieci tentativi falliti"; exit 1; }
  echo "$(date -u +%T) passo $PASSO: lotto di $n"
  [ "$n" -lt "$LOTTO" ] && break
done
$PSQL -c "SELECT fase, count(*) FROM ripasso_v1_dal_titolo GROUP BY 1 ORDER BY 1"
echo FINE-PASSO-$PASSO
