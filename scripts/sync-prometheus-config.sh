#!/bin/bash
# Copy Prometheus config into the running container (Colima-safe).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

SRC="$ROOT/prometheus/config/prometheus.yml"
DEST="$ROOT/data/prometheus/prometheus.yml"
mkdir -p "$(dirname "$DEST")"
cp -f "$SRC" "$DEST"
echo "Synced to host: $DEST"

COMPOSE="docker-compose"
command -v docker-compose >/dev/null 2>&1 || COMPOSE="docker compose"

CID=$($COMPOSE ps -q prometheus 2>/dev/null || true)
if [[ -z "$CID" ]]; then
  echo "Prometheus container not running; start stack then re-run."
  exit 1
fi

docker exec "$CID" mkdir -p /etc/prometheus
docker cp "$SRC" "$CID:/etc/prometheus/prometheus.yml"
docker restart "$CID"
echo "Prometheus restarted with config from $SRC"
