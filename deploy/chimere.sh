#!/usr/bin/env bash
# Sana le chimere: nuova chiave (piattaforma, tenant, id), via le righe con
# l'URL di un altro tenant. Idempotente. Da lanciare UNA volta sul server,
# con i runner fermi (li riavvia lui).
set -uo pipefail
PSQL="docker exec -i nivult-db-1 psql -U nivult -d nivult_ats -v ON_ERROR_STOP=1"
echo "── indice unico nuovo (fuori transazione, non blocca le letture)"
$PSQL -c "CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS ats_jobs_platform_slug_external_idx ON ats_jobs (platform_id, slug, external_id);"
echo "── vincolo nuovo, via il vecchio"
$PSQL -c "DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ats_jobs_platform_slug_external_key') THEN
    ALTER TABLE ats_jobs ADD CONSTRAINT ats_jobs_platform_slug_external_key UNIQUE USING INDEX ats_jobs_platform_slug_external_idx;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ats_jobs_platform_id_external_id_key') THEN
    ALTER TABLE ats_jobs DROP CONSTRAINT ats_jobs_platform_id_external_id_key;
  END IF;
END \$\$;"
echo "── riavvio dei runner col codice nuovo (ON CONFLICT sulla chiave nuova)"
for u in nivult-scrape nivult-scrape-veloce nivult-profonda nivult-volano; do systemctl restart $u; done
echo "── via le chimere: righe il cui URL non contiene il tenant, sulle piattaforme con id per tenant"
$PSQL -c "DELETE FROM ats_jobs WHERE platform_id IN ('icims','workday','cornerstone','eploy','traffit','pinpoint','vincere','hiringthing') AND url NOT ILIKE '%' || slug || '%';"
$PSQL -c "SELECT platform_id, count(*) attive, count(*) FILTER (WHERE url NOT ILIKE '%'||slug||'%') chimere FROM ats_jobs WHERE expired_at IS NULL AND platform_id IN ('icims','workday','cornerstone','eploy','traffit','pinpoint','vincere','hiringthing') GROUP BY 1 ORDER BY 2 DESC;"
echo "── fatto"
