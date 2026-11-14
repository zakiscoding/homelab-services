#!/usr/bin/env bash
# Copies the tracing / SLO / alerting config into the running containers and restarts them.
# Same approach as sync-prometheus-config.sh: Colima can't bind-mount from the external drive.
#
#   docker-compose up -d otel-collector tempo alertmanager blackbox   # first time
#   ./scripts/sync-observability.sh
#
# Alertmanager's webhook URL comes from ALERT_WEBHOOK_URL in .env and is written to a file
# inside the container only; it is never printed or committed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
COMPOSE="docker-compose"
command -v docker-compose >/dev/null 2>&1 || COMPOSE="docker compose"

cid() { $COMPOSE ps -q "$1" 2>/dev/null || true; }
# Copies a file to <dir>/<name> in a container. Distroless images have no shell or mkdir, and
# docker cp won't create a missing parent, so the file is staged in a directory named like the
# target and the whole directory is copied (which creates it).
put() { # service, source, target-dir, target-name
  local c stage; c=$(cid "$1")
  [[ -n "$c" ]] || { echo "$1 is not running; start it first" >&2; exit 1; }
  stage=$(mktemp -d); mkdir -p "$stage/$(basename "$3")"
  cp "$2" "$stage/$(basename "$3")/$4"; chmod -R go-rwx "$stage"
  docker cp "$stage/$(basename "$3")" "$c:$(dirname "$3")/"
  rm -rf "$stage"
}

put otel-collector observability/otel-collector.yaml /etc/otelcol-contrib config.yaml
put tempo observability/tempo.yaml /etc/tempo tempo.yaml
put blackbox observability/blackbox.yml /etc/blackbox blackbox.yml
put alertmanager observability/alertmanager.yml /etc/alertmanager alertmanager.yml

URL=$(grep -E '^ALERT_WEBHOOK_URL=' .env 2>/dev/null | head -1 | cut -d= -f2- || true)
TMP=$(mktemp); chmod 600 "$TMP"
if [[ -n "$URL" ]]; then printf '%s' "$URL" > "$TMP"; else
  echo "ALERT_WEBHOOK_URL not set in .env: alerts stay in the Alertmanager UI only" >&2
  printf 'http://127.0.0.1:9/no-webhook-configured' > "$TMP"
fi
put alertmanager "$TMP" /etc/alertmanager/secrets webhook_url
rm -f "$TMP"

# Prometheus rules (prometheus.yml itself goes through sync-prometheus-config.sh)
docker cp prometheus/config/rules "$(cid prometheus):/etc/prometheus/"

for s in otel-collector tempo blackbox alertmanager; do docker restart "$(cid $s)" >/dev/null; done
"$ROOT/scripts/sync-prometheus-config.sh"
echo "observability config synced"
