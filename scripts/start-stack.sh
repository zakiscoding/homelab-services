#!/bin/bash
# Start Colima (if needed) and homelab-services stack on the 1TB drive.
set -euo pipefail

# Colima/Lima bind mounts work more reliably via a path without spaces.
LINK="$HOME/1tb-drive"
SOURCE="/Volumes/1tb drive"
[[ -L "$LINK" ]] || ln -sf "$SOURCE" "$LINK"

export COLIMA_HOME="$SOURCE/colima"
export DOCKER_CONFIG="$SOURCE/docker-config"
mkdir -p "$COLIMA_HOME" "$DOCKER_CONFIG"

if [[ ! -f "$DOCKER_CONFIG/config.json" ]]; then
  cat > "$DOCKER_CONFIG/config.json" << 'JSON'
{
  "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
}
JSON
fi

if ! colima status 2>&1 | grep -q "Running"; then
  echo "Starting Colima (VM data: $COLIMA_HOME)..."
  colima start --cpu 4 --memory 6 --disk 80 --mount-type virtiofs --mount "$SOURCE:w"
fi

docker context use colima 2>/dev/null || true

SERVICES="$LINK/homelab-services"
cd "$SERVICES"

docker-compose pull
docker-compose up -d --build
docker-compose ps
sleep 3
curl -sf http://localhost/health && echo "" || echo "Health check pending — run: docker-compose logs api caddy"
