#!/usr/bin/env bash
# Clone homelab-web beside homelab-services on the 1TB drive (required for docker build context).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PARENT="$(dirname "$ROOT")"
WEB="$PARENT/homelab-web"
REPO="${HOMELAB_WEB_REPO:-https://github.com/zakiscoding/homelab-web.git}"

if [[ -f "$WEB/package.json" ]]; then
  echo "homelab-web already present at $WEB"
  exit 0
fi

echo "Cloning homelab-web to $WEB ..."
git clone "$REPO" "$WEB"
echo "Done. Build with: docker-compose up -d --build web"
