#!/usr/bin/env bash
# Inject Caddyfile into the running container (Colima-safe). Use when DOMAIN is set.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

CFG="$ROOT/data/caddy/Caddyfile"
if [[ ! -f "$CFG" ]]; then
  echo "Missing $CFG — run ./scripts/render-caddyfile.sh first"
  exit 1
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:${PATH:-}"
# shellcheck source=docker-1tb-env.sh
source "$SCRIPT_DIR/docker-1tb-env.sh"

COMPOSE="docker-compose"
command -v docker-compose >/dev/null 2>&1 || COMPOSE="docker compose"

CID=$($COMPOSE ps -q caddy 2>/dev/null || true)
if [[ -z "$CID" ]]; then
  echo "Caddy container not running; start stack first."
  exit 1
fi

docker cp "$CFG" "$CID:/etc/caddy/Caddyfile"
docker exec "$CID" caddy reload --config /etc/caddy/Caddyfile 2>/dev/null \
  || docker restart "$CID"
echo "Caddy reloaded with $CFG"
