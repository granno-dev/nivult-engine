#!/usr/bin/env bash
# Lettura sola per il ponte sul database dell'ATS.
#
# Il sistema ATS gira come superutente `nivult` e possiede `nivult_ats`.
# Il ponte NON deve poterci scrivere: qui si concede a `nivult_app` il
# SELECT sulle tabelle che legge. Il vincolo «il ponte non tocca l'ATS»
# diventa così un privilegio del database, non una promessa del codice.
# Per Mavult le tabelle sono SOLO quelle del ponte: niente cruscotto e
# revoca preventiva di eventuali grant ampi lasciati da provisioning vecchi.
#
# NON è una migrazione: `nivult_ats` è gestito dall'altro sistema e il suo
# schema non ci appartiene. È un'assegnazione di permessi, versionata qui
# perché sia ripetibile invece che fatta a mano una volta e dimenticata.
#
# Niente ALTER DEFAULT PRIVILEGES, deliberatamente: darebbe lettura su
# qualunque tabella l'ATS creerà in futuro, senza che nessuno lo decida.
# Se un domani il ponte avrà bisogno di una tabella in più, si aggiunge qui.
#
#   ./deploy/ponte-ats-grants.sh                   # applica per Nivult
#   ./deploy/ponte-ats-grants.sh --check           # verifica Nivult
#   ./deploy/ponte-ats-grants.sh --mavult          # applica per Mavult
#   ./deploy/ponte-ats-grants.sh --mavult --check  # verifica Mavult
set -euo pipefail

CONTAINER="${CONTAINER:-nivult-db-1}"
RUOLO=nivult_app
CHECK=0
for arg in "$@"; do
  case "$arg" in
    --mavult) RUOLO=mavult_ats_read ;;
    --check) CHECK=1 ;;
    *) printf 'Uso: %s [--mavult] [--check]\n' "$0" >&2; exit 2 ;;
  esac
done

TABELLE=(ats_jobs job_classifications ats_companies)
if [[ "$RUOLO" == nivult_app ]]; then
  # Il cruscotto Nivult legge anche queste viste/tabelle; Mavult non le
  # riceve mai. Senza questi grant il pannello Nivult mostrava trattini.
  TABELLE+=(azienda_skill azienda_paese stipendi_benchmark
            company_domains iso_paesi organizations
            mv_cruscotto_copertura mv_cruscotto_non_class)
fi

psql_ats() { docker exec -i "$CONTAINER" psql -U nivult -d nivult_ats -tAc "$1"; }

if [[ "$CHECK" -eq 1 ]]; then
  esito=0
  for t in "${TABELLE[@]}"; do
    ok=$(psql_ats "SELECT has_table_privilege('$RUOLO', '$t', 'SELECT')")
    scrive=$(psql_ats "SELECT has_table_privilege('$RUOLO', '$t', 'INSERT') OR has_table_privilege('$RUOLO', '$t', 'UPDATE') OR has_table_privilege('$RUOLO', '$t', 'DELETE')")
    printf '  %-22s legge=%s scrive=%s\n' "$t" "$ok" "$scrive"
    [[ "$ok" == "t" && "$scrive" == "f" ]] || esito=1
  done
  if [[ "$RUOLO" == mavult_ats_read ]]; then
    altri=$(psql_ats "SELECT coalesce(string_agg(DISTINCT table_name, ','), '') FROM information_schema.role_table_grants WHERE grantee = 'mavult_ats_read' AND table_schema = 'public' AND table_name NOT IN ('ats_jobs', 'job_classifications', 'ats_companies')")
    if [[ -n "$altri" ]]; then
      printf '  grant Mavult fuori perimetro: %s\n' "$altri"
      esito=1
    fi
  fi
  if [[ $esito -eq 0 ]]; then echo "OK: lettura sola e perimetro rispettati"; else echo "NON conforme"; fi
  exit $esito
fi

psql_ats "GRANT CONNECT ON DATABASE nivult_ats TO $RUOLO" >/dev/null
psql_ats "GRANT USAGE ON SCHEMA public TO $RUOLO" >/dev/null
if [[ "$RUOLO" == mavult_ats_read ]]; then
  psql_ats "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM mavult_ats_read" >/dev/null
  psql_ats "REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM mavult_ats_read" >/dev/null
fi
for t in "${TABELLE[@]}"; do
  psql_ats "GRANT SELECT ON TABLE $t TO $RUOLO" >/dev/null
  echo "  SELECT su $t per $RUOLO"
done
echo "fatto"
