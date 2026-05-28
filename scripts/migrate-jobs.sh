#!/usr/bin/env bash
# Apply 002_jobs.sql to existing Postgres (init.d only runs on first volume init).
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f ./scripts/docker-1tb-env.sh ]]; then
  # shellcheck source=/dev/null
  source ./scripts/docker-1tb-env.sh
fi

: "${POSTGRES_USER:=app}"
: "${POSTGRES_DB:=homelab}"

echo "Applying postgres/002_jobs.sql ..."
docker-compose exec -T postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" < postgres/002_jobs.sql
echo "Done."
