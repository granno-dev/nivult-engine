#!/usr/bin/env bash
# Lettura sola per il ponte sul database dell'ATS.
#
# Il sistema ATS gira come superutente `nivult` e possiede `nivult_ats`.
# Il ponte NON deve poterci scrivere: qui si concede a `nivult_app` il
# SELECT sulle tre sole tabelle che legge. Il vincolo «il ponte non tocca
# l'ATS» diventa così un privilegio del database, non una promessa del
# codice.
#
# NON è una migrazione: `nivult_ats` è gestito dall'altro sistema e il suo
# schema non ci appartiene. È un'assegnazione di permessi, versionata qui
# perché sia ripetibile invece che fatta a mano una volta e dimenticata.
#
# Niente ALTER DEFAULT PRIVILEGES, deliberatamente: darebbe lettura su
# qualunque tabella l'ATS creerà in futuro, senza che nessuno lo decida.
# Se un domani il ponte avrà bisogno di una tabella in più, si aggiunge qui.
#
#   ./deploy/ponte-ats-grants.sh          # applica
#   ./deploy/ponte-ats-grants.sh --check  # verifica soltanto
set -euo pipefail

CONTAINER="${CONTAINER:-nivult-db-1}"
TABELLE=(ats_jobs job_classifications ats_companies)

psql_ats() { docker exec -i "$CONTAINER" psql -U nivult -d nivult_ats -tAc "$1"; }

if [[ "${1:-}" == "--check" ]]; then
  esito=0
  for t in "${TABELLE[@]}"; do
    ok=$(psql_ats "SELECT has_table_privilege('nivult_app', '$t', 'SELECT')")
    scrive=$(psql_ats "SELECT has_table_privilege('nivult_app', '$t', 'INSERT') OR has_table_privilege('nivult_app', '$t', 'UPDATE') OR has_table_privilege('nivult_app', '$t', 'DELETE')")
    printf '  %-22s legge=%s scrive=%s\n' "$t" "$ok" "$scrive"
    [[ "$ok" == "t" && "$scrive" == "f" ]] || esito=1
  done
  if [[ $esito -eq 0 ]]; then echo "OK: lettura sola su tutte"; else echo "NON conforme"; fi
  exit $esito
fi

psql_ats "GRANT CONNECT ON DATABASE nivult_ats TO nivult_app" >/dev/null
for t in "${TABELLE[@]}"; do
  psql_ats "GRANT SELECT ON TABLE $t TO nivult_app" >/dev/null
  echo "  SELECT su $t"
done
echo "fatto"
