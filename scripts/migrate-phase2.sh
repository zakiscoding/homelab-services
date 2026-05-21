#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ -f ./scripts/docker-1tb-env.sh ]]; then
  # shellcheck source=/dev/null
  source ./scripts/docker-1tb-env.sh
fi
: "${POSTGRES_USER:=app}"
: "${POSTGRES_DB:=homelab}"
echo "Applying postgres/005_phase2_rag.sql ..."
docker-compose exec -T postgres psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" < postgres/005_phase2_rag.sql
echo "Done."
