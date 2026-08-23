#!/usr/bin/env bash
# Riepilogo del giro. Sola lettura.
#   /opt/nivult/report.sh            ultimo giorno
#   /opt/nivult/report.sh --days 7   ultima settimana
#   /opt/nivult/report.sh --check    solo allarmi, esce 1 se ce n'è
exec /opt/nivult/engine/.venv/bin/python -m nivult.report "$@"
