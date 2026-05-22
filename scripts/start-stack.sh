#!/bin/bash
# Start Colima (if needed) and homelab-services stack on the 1TB drive.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=docker-1tb-env.sh
source "$SCRIPT_DIR/docker-1tb-env.sh"

SERVICES="/Volumes/1tb drive/homelab-services"
cd "$SERVICES"

if ! colima status 2>&1 | grep -q "Running"; then
  echo "Starting Colima..."
  colima start --cpu 4 --memory 6 --disk 80 --mount-type virtiofs --mount "/Volumes/1tb drive:w"
fi

bash "$SCRIPT_DIR/render-caddyfile.sh"
docker-compose pull
docker-compose up -d --build
docker-compose ps
sleep 5
bash "$SCRIPT_DIR/sync-prometheus-config.sh" || true
bash "$SCRIPT_DIR/sync-grafana-provisioning.sh" || true
curl -sf http://localhost/ >/dev/null && echo "Web UI OK (http://localhost/)" || echo "Web check pending — run: docker-compose logs web caddy"
docker-compose exec -T api wget -qO- http://127.0.0.1:8000/health 2>/dev/null && echo "API OK" || true
