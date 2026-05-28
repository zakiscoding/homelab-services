#!/usr/bin/env bash
# Apply 006_distributed_train.sql to Postgres on iMac.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
source "$SCRIPT_DIR/docker-1tb-env.sh" 2>/dev/null || true
SQL="$ROOT/postgres/006_distributed_train.sql"
docker compose -f "$ROOT/docker-compose.yml" exec -T postgres \
  psql -U "${POSTGRES_USER:-app}" -d "${POSTGRES_DB:-homelab}" < "$SQL"
echo "006_distributed_train.sql applied."
