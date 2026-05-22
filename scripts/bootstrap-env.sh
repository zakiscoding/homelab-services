#!/usr/bin/env bash
# Generate homelab-services/.env from .env.example (never committed).
# Usage: ./scripts/bootstrap-env.sh [optional-domain-fqdn]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"
DOMAIN_ARG="${1:-}"
SITE_LABEL="${SITE_LABEL:-ml-zakis-console}"

if [[ -f .env ]]; then
  echo ".env already exists — delete to regenerate, or edit in place."
  exit 0
fi

API_KEY=$(openssl rand -hex 24)
WORKER_KEY=$(openssl rand -hex 24)
PG_PASS=$(openssl rand -hex 16)
GF_PASS=$(openssl rand -hex 12)
WEB_USER="homelab"
WEB_PASS=$(openssl rand -base64 12 | tr -d '/+=' | head -c 16)

cp .env.example .env
sed -i '' "s/change-me-long-random-string/$API_KEY/" .env 2>/dev/null || sed -i "s/change-me-long-random-string/$API_KEY/" .env
sed -i '' "s/change-me-worker-key-different-from-api-key/$WORKER_KEY/" .env 2>/dev/null || sed -i "s/change-me-worker-key-different-from-api-key/$WORKER_KEY/" .env
sed -i '' "s/change-me-strong/$PG_PASS/" .env 2>/dev/null || sed -i "s/change-me-strong/$PG_PASS/" .env
sed -i '' "s/GRAFANA_PASSWORD=change-me/GRAFANA_PASSWORD=$GF_PASS/" .env 2>/dev/null || sed -i "s/GRAFANA_PASSWORD=change-me/GRAFANA_PASSWORD=$GF_PASS/" .env

WEB_HASH=""
if command -v docker &>/dev/null; then
  WEB_HASH=$(docker run --rm caddy:2-alpine caddy hash-password --plaintext "$WEB_PASS" | tr -d '\r')
fi

{
  echo ""
  echo "# --- Public site ($SITE_LABEL) ---"
  echo "# Browser login: user=$WEB_USER  password=$WEB_PASS"
  echo "WEB_AUTH_USER=$WEB_USER"
  [[ -n "$WEB_HASH" ]] && echo "WEB_AUTH_HASH='$WEB_HASH'"
  if [[ -n "$DOMAIN_ARG" ]]; then
    echo "DOMAIN=$DOMAIN_ARG"
    echo "ACME_EMAIL=admin@${DOMAIN_ARG#*.}"
  fi
} >> .env

echo "Created $ROOT/.env"
echo "  Web login: $WEB_USER / $WEB_PASS"
[[ -z "$DOMAIN_ARG" ]] && echo "  DOMAIN not set — add your real FQDN for public HTTPS, then ./scripts/render-caddyfile.sh"

PARENT="$(dirname "$ROOT")"
if [[ -f "$PARENT/homelab-web/.env.local.example" ]]; then
  cat > "$PARENT/homelab-web/.env.local" <<EOF
HOMELAB_API_URL=http://100.91.113.98
HOMELAB_API_KEY=$API_KEY
EOF
  echo "  Wrote $PARENT/homelab-web/.env.local"
fi
