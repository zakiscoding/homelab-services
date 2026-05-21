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

docker-compose pull
docker-compose up -d --build
docker-compose ps
sleep 3
curl -sf http://localhost/health && echo "" || echo "Health check pending — run: docker-compose logs api caddy"
