#!/usr/bin/env bash
# End-to-end proof on a throwaway k3d cluster in Docker: helm install (chart-testing style),
# then Argo CD syncing the chart from an in-cluster git server, then a selfHeal check.
#
#   bash charts/test-k3d.sh all        # every step below, in order
#   bash charts/test-k3d.sh <step>     # create | images | helm | argocd | selfheal | delete
#
# Only Docker and kubectl are needed on the host. k3d, helm and the argocd CLI run from their
# official images. The cluster gets its own name and host ports so it can share Docker with
# other clusters: API 127.0.0.1:18643, ingress 127.0.0.1:18680.
#
# Assumes ../homelab-web is checked out next to this repo (the web image build context).
# Secrets are random throwaway values generated here; they are never printed or written to
# disk outside the cluster.
set -euo pipefail
export MSYS_NO_PATHCONV=1

CLUSTER=hs-gitops
K3S_IMAGE=rancher/k3s:v1.36.6-k3s1
K3D_IMAGE=ghcr.io/k3d-io/k3d:5.9.0
HELM_IMAGE=alpine/helm:4.3.2
ARGOCD_CLI_IMAGE=quay.io/argoproj/argocd:v3.6.1
API_PORT=18643
INGRESS_PORT=18680
BRANCH="$(git -C "$(dirname "$0")" rev-parse --abbrev-ref HEAD)"

here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/.." && pwd)"
# Scratch dir (kubeconfigs, bare clone). A fixed path so the steps can also run one by one.
work="${WORK_DIR:-${TMPDIR:-/tmp}/hs-gitops-k3d}"
mkdir -p "$work"
winpath() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else echo "$1"; fi; }

export KUBECONFIG="$work/kubeconfig"
[[ -n "${MSYSTEM:-}" ]] && export KUBECONFIG="$(winpath "$KUBECONFIG")"
kubectl() { command kubectl "$@"; }
k3d() { docker run --rm -v /var/run/docker.sock:/var/run/docker.sock "$K3D_IMAGE" "$@"; }
# helm runs on the k3d Docker network and talks to the server node directly
helm() {
  docker run --rm --network "k3d-$CLUSTER" \
    -v "$(winpath "$work"):/work" -v "$(winpath "$here"):/charts" -w /charts \
    -e KUBECONFIG=/work/kubeconfig.internal "$HELM_IMAGE" "$@"
}
ts() { date +%s; }
log() { printf '\n== %s\n' "$*"; }

rand_hex() { openssl rand -hex "${1:-24}"; }

create_secret() {  # $1 namespace
  kubectl create namespace "$1" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl -n "$1" create secret generic homelab-secrets \
    --from-literal=POSTGRES_USER=app \
    --from-literal=POSTGRES_PASSWORD="$(rand_hex)" \
    --from-literal=POSTGRES_DB=homelab \
    --from-literal=API_KEY="$(rand_hex)" \
    --from-literal=WORKER_API_KEY="$(rand_hex)" \
    --from-literal=GRAFANA_USER=admin \
    --from-literal=GRAFANA_PASSWORD="$(rand_hex 16)" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  echo "secret homelab-secrets created in $1 (random values, not shown)"
}

step_create() {
  log "create k3d cluster $CLUSTER ($K3S_IMAGE)"
  local t0; t0=$(ts)
  k3d cluster create "$CLUSTER" --image "$K3S_IMAGE" \
    --api-port "127.0.0.1:$API_PORT" -p "127.0.0.1:$INGRESS_PORT:80@loadbalancer" \
    --servers 1 --agents 0 --kubeconfig-update-default=false --wait --timeout 300s
  echo "cluster created in $(( $(ts) - t0 ))s"
  write_kubeconfigs
  kubectl wait --for=condition=Ready node --all --timeout=120s
}

write_kubeconfigs() {
  k3d kubeconfig get "$CLUSTER" > "$work/kubeconfig"
  # The same cluster as seen from containers on the k3d network (helm, argocd CLI)
  sed "s#https://127.0.0.1:$API_PORT#https://k3d-$CLUSTER-server-0:6443#" "$work/kubeconfig" > "$work/kubeconfig.internal"
  # argocd --core reads the namespace from the kubeconfig context
  kubectl --kubeconfig "$(winpath "$work/kubeconfig.internal")" config set-context --current --namespace=argocd >/dev/null
}

step_images() {
  log "build api/worker/web images and import them into k3d"
  local t0; t0=$(ts)
  docker build -q -t homelab-api:local "$repo/api"
  docker build -q -t homelab-worker:local -f "$repo/worker/Dockerfile" "$repo"
  # Clean export of homelab-web (its Dockerfile copies the whole context)
  rm -rf "$work/web-src" && mkdir -p "$work/web-src"
  git -C "$repo/../homelab-web" archive HEAD | tar -x -C "$work/web-src"
  docker build -q -t homelab-web:local "$(winpath "$work/web-src")"
  k3d image import -c "$CLUSTER" homelab-api:local homelab-worker:local homelab-web:local
  echo "images ready in $(( $(ts) - t0 ))s"
}

pod_summary() {  # $1 namespace
  kubectl -n "$1" get pods -o wide
  local total ready
  total=$(kubectl -n "$1" get pods --no-headers | wc -l)
  ready=$(kubectl -n "$1" get pods --no-headers | awk '{split($2,a,"/"); if (a[1]==a[2] && $3=="Running") n++} END {print n+0}')
  echo "pods ready: $ready/$total"
}

smoke() {  # $1 namespace
  log "smoke checks in $1"
  kubectl -n "$1" run smoke --rm -i --restart=Never --image=curlimages/curl:8.17.0 --quiet -- sh -c '
    for u in http://api:8000/health http://api:8000/health/gpu http://qdrant:6333/readyz \
             http://prometheus:9090/-/ready http://pushgateway:9091/-/ready \
             http://grafana:3000/api/health http://mlflow:5000/health http://web:3000/; do
      printf "%-36s %s\n" "$u" "$(curl -s -o /dev/null -m 45 -w "%{http_code} in %{time_total}s" "$u")"
    done'
  echo "ingress (Traefik -> web): $(curl -s -o /dev/null -m 45 -w '%{http_code} in %{time_total}s' "http://127.0.0.1:$INGRESS_PORT/")"
  # Prometheus scrape targets. In-cluster ones must be up; the tailnet ones are up only if the
  # Docker host is on the tailnet and that node is online.
  local targets
  targets=$(kubectl -n "$1" exec prometheus-0 -- wget -qO- 'http://localhost:9090/api/v1/targets?state=active')
  echo "prometheus targets: $(grep -o '"health":"up"' <<<"$targets" | wc -l) up, $(grep -o '"health":"down"' <<<"$targets" | wc -l) down"
  grep -oE '"(scrapePool|health)":"[^"]*"' <<<"$targets" | cut -d'"' -f4 | paste - - | sort | uniq -c
}

step_helm() {
  local ns=homelab-helm
  log "helm install into $ns (chart-testing style: install, wait, check, uninstall)"
  create_secret "$ns"
  local t0; t0=$(ts)
  helm install homelab homelab -n "$ns" -f homelab/values-local.yaml --wait --timeout 10m
  echo "helm install --wait finished in $(( $(ts) - t0 ))s"
  pod_summary "$ns"
  smoke "$ns"
  helm uninstall homelab -n "$ns" --wait
  kubectl delete namespace "$ns" --wait=true
}

refresh_git_server() {
  rm -rf "$work/homelab-services.git"
  git clone -q --bare --branch "$BRANCH" "$repo" "$work/homelab-services.git"
  local pod; pod=$(kubectl -n gitserver get pod -l app=git-server -o jsonpath='{.items[0].metadata.name}')
  tar -C "$work" -cf "$work/repo.tar" homelab-services.git
  kubectl -n gitserver cp "$(winpath "$work/repo.tar")" "$pod:/tmp/repo.tar"
  kubectl -n gitserver exec "$pod" -- sh -c 'rm -rf /srv/git/homelab-services.git && tar -C /srv/git -xf /tmp/repo.tar'
  echo "git server serves $BRANCH at $(git -C "$repo" rev-parse --short HEAD)"
}

argocd_cli() {
  docker run --rm --network "k3d-$CLUSTER" -v "$(winpath "$work"):/work" \
    -e KUBECONFIG=/work/kubeconfig.internal --entrypoint argocd "$ARGOCD_CLI_IMAGE" \
    --core "$@"
}

step_argocd() {
  log "install Argo CD (pinned upstream manifest)"
  local t0; t0=$(ts)
  kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  kubectl apply --server-side --force-conflicts -k "$(winpath "$repo/argocd/install")" >/dev/null
  kubectl -n argocd rollout status deploy/argocd-repo-server --timeout=5m
  kubectl -n argocd rollout status deploy/argocd-server --timeout=5m
  kubectl -n argocd rollout status statefulset/argocd-application-controller --timeout=5m
  echo "Argo CD ready in $(( $(ts) - t0 ))s"

  log "in-cluster git server"
  kubectl apply -f "$(winpath "$repo/argocd/overlays/local-k3d/git-server.yaml")"
  kubectl -n gitserver rollout status deploy/git-server --timeout=2m
  refresh_git_server

  log "register AppProject + Application (overlay local-k3d)"
  create_secret homelab
  t0=$(ts)
  kubectl apply -k "$(winpath "$repo/argocd/overlays/local-k3d")"
  until [[ "$(kubectl -n argocd get application homelab -o jsonpath='{.status.sync.status}/{.status.health.status}' 2>/dev/null)" == "Synced/Healthy" ]]; do
    (( $(ts) - t0 > 900 )) && { echo "timed out"; kubectl -n argocd get application homelab -o yaml | tail -40; return 1; }
    sleep 5
  done
  echo "Application Synced/Healthy $(( $(ts) - t0 ))s after it was created"
  kubectl -n argocd get application homelab
  argocd_cli app get homelab --app-namespace argocd 2>/dev/null || true
  pod_summary homelab
  smoke homelab
}

step_selfheal() {
  log "selfHeal: scale api to 0 (git says 1)"
  local t0 r
  kubectl -n homelab scale deploy/api --replicas=0
  t0=$(ts)
  until [[ "$(kubectl -n homelab get deploy api -o jsonpath='{.spec.replicas}')" == "1" ]]; do sleep 1; done
  r=$(( $(ts) - t0 ))
  echo "spec.replicas reverted to 1 after ${r}s"
  kubectl -n homelab rollout status deploy/api --timeout=3m
  echo "api serving again $(( $(ts) - t0 ))s after the scale-down"

  log "selfHeal: delete the worker Deployment"
  kubectl -n homelab delete deploy worker --wait=true
  t0=$(ts)
  until kubectl -n homelab get deploy worker >/dev/null 2>&1; do sleep 1; done
  echo "worker Deployment recreated after $(( $(ts) - t0 ))s"
  kubectl -n homelab rollout status deploy/worker --timeout=3m
  echo "worker Ready $(( $(ts) - t0 ))s after the delete"

  log "selfHeal: edit the Prometheus ConfigMap by hand"
  kubectl -n homelab patch configmap prometheus-config --type merge -p '{"data":{"prometheus.yml":"global: {}"}}'
  t0=$(ts)
  until kubectl -n homelab get configmap prometheus-config -o jsonpath='{.data.prometheus\.yml}' | grep -q homelab-api; do sleep 1; done
  echo "prometheus-config restored from git after $(( $(ts) - t0 ))s"

  kubectl -n argocd get application homelab -o jsonpath='{.status.sync.status}/{.status.health.status}{"\n"}'
  kubectl -n argocd get application homelab -o jsonpath='{range .status.history[*]}{.id}{"  "}{.revision}{"  "}{.deployedAt}{"\n"}{end}'
}

step_delete() {
  log "delete k3d cluster $CLUSTER"
  k3d cluster delete "$CLUSTER"
}

case "${1:-all}" in
  create) step_create ;;
  images) step_images ;;
  helm) write_kubeconfigs; step_helm ;;
  argocd) write_kubeconfigs; step_argocd ;;
  selfheal) step_selfheal ;;
  delete) step_delete ;;
  all) step_create; step_images; step_helm; step_argocd; step_selfheal; step_delete ;;
  *) echo "usage: $0 [all|create|images|helm|argocd|selfheal|delete]"; exit 2 ;;
esac
