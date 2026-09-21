#!/bin/bash
# RIEMPIRE I VUOTI (21/09/2026). Con le soglie al 95% v1 lasciava vuoti
# seniority, contratto, remoto (e a volte la famiglia) nella maggior parte
# delle offerte gia' viste. Ora che le soglie sono a 0,5, quelle offerte vanno
# rilette: entrano nella tabella del ripasso (fase 5, cosi' non si confondono
# con le 793k del titolo) e locale_v1_at torna NULL a lotti da 2.000. Il
# demone le legge dalla tabella senza ordinare, tre lotti su quattro.
set -u
LOTTO=${LOTTO:-2000}
PSQL="docker exec -i nivult-db-1 psql -U nivult -d nivult_ats -At -v ON_ERROR_STOP=1"
$PSQL -c "SET statement_timeout = '60min';
INSERT INTO ripasso_v1_dal_titolo (job_id, fase)
SELECT j.id, 5 FROM ats_jobs j
 WHERE j.expired_at IS NULL AND j.locale_v1_at IS NOT NULL
   AND (j.seniority IS NULL OR j.employment_type IS NULL OR j.remote IS NULL
        OR NOT EXISTS (SELECT 1 FROM job_classifications x WHERE x.job_id = j.id AND coalesce(x.family, x.v1_family) IS NOT NULL))
ON CONFLICT (job_id) DO NOTHING;" && $PSQL -c "SELECT count(*) AS da_riempire FROM ripasso_v1_dal_titolo WHERE fase = 5"
while :; do
  SQL="BEGIN; CREATE TEMP TABLE lotto AS SELECT job_id FROM ripasso_v1_dal_titolo WHERE fase = 5 LIMIT $LOTTO;
       UPDATE ats_jobs j SET locale_v1_at = NULL FROM lotto l WHERE j.id = l.job_id;
       UPDATE ripasso_v1_dal_titolo r SET fase = 6 FROM lotto l WHERE r.job_id = l.job_id;
       SELECT count(*) FROM lotto; COMMIT;"
  n=""
  for t in 1 2 3 4 5 6 7 8 9 10; do
    n=$($PSQL -c "$SQL" 2>/tmp/ripasso-vuoti-err.log | grep -E '^[0-9]+$' | tail -1) && [ -n "$n" ] && break
    echo "$(date -u +%T) lotto fallito (tentativo $t): $(tail -1 /tmp/ripasso-vuoti-err.log)"; sleep $((5 * t))
  done
  [ -z "$n" ] && { echo "mi fermo: dieci tentativi falliti"; exit 1; }
  echo "$(date -u +%T) in coda $n"
  [ "$n" -lt "$LOTTO" ] && break
done
$PSQL -c "SELECT fase, count(*) FROM ripasso_v1_dal_titolo GROUP BY 1 ORDER BY 1"
echo FINE-RIPASSO-VUOTI
