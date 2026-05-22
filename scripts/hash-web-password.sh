#!/usr/bin/env bash
# Print bcrypt hash for Caddy basicauth (paste into .env as WEB_AUTH_HASH).
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 'your-password'" >&2
  exit 1
fi

docker run --rm caddy:2-alpine caddy hash-password --plaintext "$1"
