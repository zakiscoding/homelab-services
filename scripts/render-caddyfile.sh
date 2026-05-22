#!/usr/bin/env bash
# Write data/caddy/Caddyfile from .env (LAN vs public DOMAIN).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="${ROOT}/.env"
OUT="${ROOT}/data/caddy/Caddyfile"
LAN="${ROOT}/caddy/Caddyfile.lan"
TPL="${ROOT}/caddy/Caddyfile.public.tpl"

mkdir -p "$(dirname "$OUT")"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${DOMAIN:-}" ]]; then
  cp "$LAN" "$OUT"
  echo "Caddy: LAN mode (:80 → web). Set DOMAIN in .env for public HTTPS."
  exit 0
fi

for var in ACME_EMAIL WEB_AUTH_USER WEB_AUTH_HASH; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: DOMAIN is set but $var is missing in .env" >&2
    echo "Run: ./scripts/hash-web-password.sh 'your-password'" >&2
    exit 1
  fi
done

export DOMAIN ACME_EMAIL WEB_AUTH_USER WEB_AUTH_HASH
if command -v envsubst >/dev/null 2>&1; then
  envsubst '${DOMAIN} ${ACME_EMAIL} ${WEB_AUTH_USER} ${WEB_AUTH_HASH}' < "$TPL" > "$OUT"
else
  python3 - "$TPL" "$OUT" <<'PY'
import os, sys
tpl_path, out_path = sys.argv[1], sys.argv[2]
keys = ("DOMAIN", "ACME_EMAIL", "WEB_AUTH_USER", "WEB_AUTH_HASH")
text = open(tpl_path).read()
for k in keys:
    text = text.replace("${" + k + "}", os.environ[k])
open(out_path, "w").write(text)
PY
fi
echo "Caddy: public HTTPS for https://${DOMAIN} → web (basic auth enabled)."
