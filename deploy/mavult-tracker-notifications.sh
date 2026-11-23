#!/usr/bin/env bash
set -uo pipefail

export BASE=/opt/mavult/engine
export LOCK=/var/run/mavult-tracker-notifications.lock
export ENV_FILE=/opt/mavult/.env
exec "$BASE/deploy/tracker-notifications.sh"
