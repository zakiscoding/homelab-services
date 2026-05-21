#!/bin/bash
# Copy Grafana provisioning + dashboards into data/grafana (Colima-safe).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

DEST="$ROOT/data/grafana"
mkdir -p "$DEST/provisioning/datasources" "$DEST/provisioning/dashboards" "$DEST/dashboards"

cp -f grafana/provisioning/datasources/prometheus.yml "$DEST/provisioning/datasources/"
cp -f grafana/provisioning/dashboards/default.yml "$DEST/provisioning/dashboards/"
cp -f grafana/dashboards/homelab-overview.json "$DEST/dashboards/"

echo "Synced Grafana provisioning to $DEST"
ls -la "$DEST/provisioning/datasources" "$DEST/provisioning/dashboards" "$DEST/dashboards"
