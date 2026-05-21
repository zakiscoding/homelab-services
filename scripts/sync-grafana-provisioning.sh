#!/bin/bash
# Copy Grafana provisioning + dashboards into data/grafana and into the running container.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

DEST="$ROOT/data/grafana"
mkdir -p "$DEST/provisioning/datasources" "$DEST/provisioning/dashboards" "$DEST/dashboards"

cp -f grafana/provisioning/datasources/prometheus.yml "$DEST/provisioning/datasources/"
cp -f grafana/provisioning/dashboards/default.yml "$DEST/provisioning/dashboards/"
cp -f grafana/dashboards/homelab-overview.json "$DEST/dashboards/"

echo "Synced to host: $DEST"
ls -la "$DEST/provisioning/datasources" "$DEST/dashboards"

if command -v docker-compose >/dev/null 2>&1; then
  COMPOSE="docker-compose"
else
  COMPOSE="docker compose"
fi

CID=$($COMPOSE ps -q grafana 2>/dev/null || true)
if [[ -n "$CID" ]]; then
  echo "Copying into grafana container $CID ..."
  docker exec "$CID" mkdir -p /var/lib/grafana/provisioning/datasources /var/lib/grafana/provisioning/dashboards /var/lib/grafana/dashboards
  docker cp "$DEST/provisioning/datasources/prometheus.yml" "$CID:/var/lib/grafana/provisioning/datasources/prometheus.yml"
  docker cp "$DEST/provisioning/dashboards/default.yml" "$CID:/var/lib/grafana/provisioning/dashboards/default.yml"
  docker cp "$DEST/dashboards/homelab-overview.json" "$CID:/var/lib/grafana/dashboards/homelab-overview.json"
  docker restart "$CID"
  echo "Grafana restarted — dashboards should appear in folder Homelab."
else
  echo "Grafana container not running; start stack then re-run this script."
fi
