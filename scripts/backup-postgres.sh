#!/usr/bin/env bash
# Postgres backup for homelab-services. Run on iMac via cron/launchd.
set -euo pipefail
cd "$(dirname "$0")/.."
source ./scripts/docker-1tb-env.sh
mkdir -p backups
FILE="backups/homelab-$(date +%Y%m%d-%H%M).sql"
docker-compose exec -T postgres pg_dump -U "${POSTGRES_USER:-homelab}" "${POSTGRES_DB:-homelab}" > "$FILE"
find backups -name 'homelab-*.sql' -mtime +14 -delete
echo "Wrote $FILE"
