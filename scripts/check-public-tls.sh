#!/usr/bin/env bash
# Quick checks before opening the site in a browser (run on iMac or laptop).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ROOT}/.env"

read_env() {
  local key="$1"
  [[ -f "$ENV_FILE" ]] || return 0
  local line
  line="$(grep -E "^${key}=" "$ENV_FILE" | tail -1)" || return 0
  line="${line#${key}=}"
  line="${line%$'\r'}"
  line="${line#\"}"
  line="${line%\"}"
  line="${line#\'}"
  line="${line%\'}"
  printf '%s' "$line"
}

DOMAIN="$(read_env DOMAIN)"

if [[ -z "$DOMAIN" ]]; then
  echo "DOMAIN is not set in .env — public HTTPS is not configured."
  exit 1
fi

if [[ "$DOMAIN" == *"yourdomain.com"* ]]; then
  echo "ERROR: DOMAIN still looks like the docs placeholder ($DOMAIN)."
  echo "Replace it with a hostname YOU own, e.g. ml.example.com"
  exit 1
fi

echo "=== DNS (A record) ==="
if command -v dig >/dev/null 2>&1; then
  dig +short A "$DOMAIN" || true
else
  nslookup "$DOMAIN" 2>/dev/null || true
fi

echo ""
echo "=== HTTPS certificate (what the internet sees) ==="
echo | openssl s_client -connect "${DOMAIN}:443" -servername "$DOMAIN" 2>/dev/null \
  | openssl x509 -noout -subject -issuer -dates 2>/dev/null \
  || echo "Could not connect on 443 — check port-forward and Caddy logs."

echo ""
echo "=== Generated Caddyfile (on iMac) ==="
if [[ -f "${ROOT}/data/caddy/Caddyfile" ]]; then
  head -20 "${ROOT}/data/caddy/Caddyfile"
else
  echo "Missing data/caddy/Caddyfile — run ./scripts/render-caddyfile.sh"
fi

echo ""
echo "Open: https://${DOMAIN}  (not ml.yourdomain.com unless that is really your DNS name)"
