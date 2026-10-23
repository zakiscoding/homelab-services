#!/bin/bash
# Back up Postgres, then apply every migration in postgres/ (002..007) in order.
# All migrations are idempotent (IF NOT EXISTS / DROP+ADD CONSTRAINT), so re-running is safe.
# Run on the iMac with the stack up:   bash scripts/migrate-all.sh
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/.."
source "$SCRIPT_DIR/docker-1tb-env.sh" >/dev/null 2>&1 || true

PG=homelab-services-postgres-1
psql_in() { docker exec -i "$PG" sh -c 'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" '"$1"; }

mkdir -p backups
F="backups/homelab-pre-migrate-$(date +%Y%m%d-%H%M%S).sql"
docker exec "$PG" sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > "$F"
echo "backup: $F ($(wc -c < "$F") bytes)"

for f in postgres/0*.sql; do
  case "$f" in
    *007_mlflow.sql)  # CREATE DATABASE has no IF NOT EXISTS; an "already exists" error is fine
      echo "applying $f"; psql_in "" < "$f" 2>&1 | grep -v "already exists" || true ;;
    *)
      echo "applying $f"; psql_in "-v ON_ERROR_STOP=1" < "$f" ;;
  esac
done

echo "jobs columns: $(docker exec "$PG" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select string_agg(column_name, '"'"' '"'"') from information_schema.columns where table_name='"'"'jobs'"'"'"')"
echo "tables: $(docker exec "$PG" sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select string_agg(table_name, '"'"' '"'"' order by table_name) from information_schema.tables where table_schema='"'"'public'"'"'"')"
echo "Done. Restore if needed: docker exec -i $PG sh -c 'psql -U \$POSTGRES_USER -d \$POSTGRES_DB' < $F"
