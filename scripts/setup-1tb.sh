#!/bin/bash
# Run on iMac — Colima + homelab-services, all on the 1TB drive.
set -euo pipefail

DRIVE="/Volumes/1tb drive"
SERVICES="$DRIVE/homelab-services"
COLIMA_HOME="$DRIVE/colima"

export COLIMA_HOME
export DOCKER_CONFIG="$DRIVE/docker-config"
mkdir -p "$COLIMA_HOME" "$DOCKER_CONFIG" "$SERVICES/data"/{postgres,redis,qdrant,prometheus,grafana,caddy,caddy-config}

if [[ ! -f "$DOCKER_CONFIG/config.json" ]]; then
  cat > "$DOCKER_CONFIG/config.json" << 'JSON'
{
  "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
}
JSON
fi

cd "$SERVICES"

if [[ ! -f .env ]]; then
  echo "==> Creating .env from .env.example"
  cp .env.example .env
  API_KEY=$(openssl rand -hex 24)
  PG_PASS=$(openssl rand -hex 16)
  GF_PASS=$(openssl rand -hex 12)
  sed -i '' "s/change-me-long-random-string/$API_KEY/" .env
  sed -i '' "s/change-me-strong/$PG_PASS/" .env
  sed -i '' "s/GRAFANA_PASSWORD=change-me/GRAFANA_PASSWORD=$GF_PASS/" .env
  echo "    Wrote $SERVICES/.env"
fi

if ! command -v colima &>/dev/null; then
  echo "==> Install: brew install colima docker docker-compose"
  exit 1
fi

if ! colima status 2>&1 | grep -q "Running"; then
  echo "==> Starting Colima (VM disk on 1TB: $COLIMA_HOME)"
  colima start --cpu 4 --memory 6 --disk 80
fi

eval "$(colima docker-env)"
echo "==> Docker ready. Pull and start stack:"
echo "    cd \"$SERVICES\""
echo "    docker compose pull"
echo "    docker compose up -d --build"
echo "    curl http://localhost/health"
