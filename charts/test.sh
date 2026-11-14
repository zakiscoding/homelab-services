#!/usr/bin/env bash
# Static checks for charts/homelab. Needs only Docker: helm, helm-unittest and kubeconform run
# from their official images, so nothing is installed on the host.
#
#   bash charts/test.sh
#
# 1. chart file copies match the repo-root config (sync-chart-files.sh --check)
# 2. helm lint --strict with each values set
# 3. helm template + kubeconform -strict against the Kubernetes 1.36 schemas
# 4. helm-unittest suites in charts/homelab/tests/
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash on Windows: keep /apps paths as-is
here="$(cd "$(dirname "$0")" && pwd)"
mount="$here"
command -v cygpath >/dev/null 2>&1 && mount="$(cygpath -w "$here")"

HELM_IMAGE=alpine/helm:4.3.2
UNITTEST_IMAGE=helmunittest/helm-unittest:latest   # bundles helm 4.3 + helm-unittest 1.2.1
KUBECONFORM_IMAGE=ghcr.io/yannh/kubeconform:v0.8.0
K8S_VERSION=1.36.6

helm() { docker run --rm -v "$mount:/apps" -w /apps "$HELM_IMAGE" "$@"; }

value_sets=(
  "default:"
  "local:homelab/values-local.yaml"
  "minimal:homelab/ci/minimal-values.yaml"
  "all-enabled:homelab/ci/all-enabled-values.yaml"
)

echo "== 1. chart file copies"
bash "$here/sync-chart-files.sh" --check

echo "== 2. helm lint --strict"
for vs in "${value_sets[@]}"; do
  name="${vs%%:*}" file="${vs#*:}"
  echo "-- $name"
  helm lint --strict homelab ${file:+-f "$file"} | grep -E "chart\(s\) linted|ERROR|WARNING"
done

echo "== 3. helm template | kubeconform -strict (Kubernetes $K8S_VERSION)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT
for vs in "${value_sets[@]}"; do
  name="${vs%%:*}" file="${vs#*:}"
  helm template homelab homelab -n homelab ${file:+-f "$file"} > "$out/$name.yaml"
done
outm="$out"; command -v cygpath >/dev/null 2>&1 && outm="$(cygpath -w "$out")"
docker run --rm -v "$outm:/r" "$KUBECONFORM_IMAGE" \
  -strict -summary -kubernetes-version "$K8S_VERSION" -output text /r

echo "== 4. helm unittest"
docker run --rm -v "$mount:/apps" "$UNITTEST_IMAGE" --strict homelab | grep -E "PASS|FAIL|Tests:|Suites:"
