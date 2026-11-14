#!/usr/bin/env bash
# Ensure mlflow database exists (existing Postgres volumes skip init scripts).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"
source "$SCRIPT_DIR/docker-1tb-env.sh" 2>/dev/null || true

if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  COMPOSE="docker compose"
fi

USER="${POSTGRES_USER:-app}"
exists=$($COMPOSE exec -T postgres psql -U "$USER" -d postgres -tAc \
  "SELECT 1 FROM pg_database WHERE datname='mlflow'" || echo "")
if [[ "$exists" != "1" ]]; then
  $COMPOSE exec -T postgres psql -U "$USER" -d postgres -c "CREATE DATABASE mlflow;"
  echo "Created database mlflow"
else
  echo "Database mlflow already exists"
fi

mkdir -p "$ROOT/data/mlflow"
echo "Artifact dir: $ROOT/data/mlflow"
echo "Start: docker compose up -d mlflow"

