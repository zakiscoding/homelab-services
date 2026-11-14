#!/usr/bin/env bash
# Copy the repo's Prometheus, Grafana and Postgres config into the chart.
#
# Helm's .Files can only read files inside the chart directory, and symlinks are unreliable
# in a repo that is also checked out on Windows, so the chart keeps copies under
# charts/homelab/files/. The source of truth stays at the repo root (what docker-compose
# uses). Run this after editing those files; `--check` exits 1 if the copies have drifted.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
dst="$root/charts/homelab/files"

pairs=(
  "prometheus/config/prometheus.yml:prometheus/prometheus.yml"
  "grafana/provisioning/datasources/prometheus.yml:grafana/datasources/prometheus.yml"
  "grafana/provisioning/dashboards/default.yml:grafana/dashboard-providers/default.yml"
)
for f in "$root"/grafana/dashboards/*.json; do pairs+=("grafana/dashboards/$(basename "$f"):grafana/dashboards/$(basename "$f")"); done
for f in "$root"/postgres/*.sql; do pairs+=("postgres/$(basename "$f"):postgres/$(basename "$f")"); done

# Strip carriage returns (octal 015): on Windows the repo-root files may be checked out with
# CRLF, while the chart copies are always LF (charts/.gitattributes).
lf() { tr -d '\015' < "$1"; }

if [[ "${1:-}" == "--check" ]]; then
  rc=0
  for p in "${pairs[@]}"; do
    src="$root/${p%%:*}" out="$dst/${p##*:}"
    if [[ ! -f "$out" ]] || ! cmp -s <(lf "$src") <(lf "$out"); then
      echo "drift: ${p%%:*} -> charts/homelab/files/${p##*:}"; rc=1
    fi
  done
  # Copies whose source was deleted
  while IFS= read -r out; do
    rel="${out#"$dst"/}"; found=0
    for p in "${pairs[@]}"; do [[ "${p##*:}" == "$rel" ]] && found=1; done
    [[ $found == 1 ]] || { echo "stale: charts/homelab/files/$rel"; rc=1; }
  done < <(find "$dst" -type f)
  [[ $rc == 0 ]] && echo "chart files in sync (${#pairs[@]} files)"
  exit $rc
fi

rm -rf "$dst" && mkdir -p "$dst"
for p in "${pairs[@]}"; do
  out="$dst/${p##*:}"; mkdir -p "$(dirname "$out")"; lf "$root/${p%%:*}" > "$out"
done
echo "copied ${#pairs[@]} files into charts/homelab/files/"
