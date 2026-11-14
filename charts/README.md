# charts/homelab

A Helm chart for the whole homelab-services stack. It is the Kubernetes/GitOps path next to
`docker-compose.yml`, which is still what runs on the iMac. How it was tested, what Argo CD
does with it and the limits are in [docs/GITOPS.md](../docs/GITOPS.md).

| Path | Purpose |
|------|---------|
| `homelab/` | The chart: `values.yaml`, `values.schema.json`, `templates/`, `files/`, `tests/` |
| `homelab/values-local.yaml` | Throwaway k3d cluster: locally built images, small volumes |
| `homelab/ci/` | Extra value sets for lint and kubeconform (`minimal`, `all-enabled`) |
| `sync-chart-files.sh` | Copies the repo's Prometheus, Grafana and Postgres config into `homelab/files/` |
| `test.sh` | Static checks: copies in sync, `helm lint --strict`, kubeconform, helm-unittest |
| `test-k3d.sh` | Install on a local k3d cluster, then the Argo CD sync and selfHeal test |

## Components

One template per compose service. Every component has `<name>.enabled`, a pinned
`image.repository`/`image.tag`, `resources` and probes.

| Component | Kind | Image (pinned) | Storage | Probes |
|-----------|------|----------------|---------|--------|
| `postgres` | StatefulSet | `postgres:16.12-alpine` | PVC 10Gi | `pg_isready` (the compose healthcheck) |
| `redis` | StatefulSet | `redis:7.4.7-alpine` | PVC 2Gi, `--appendonly yes` | `redis-cli ping` |
| `qdrant` | StatefulSet | `qdrant/qdrant:v1.20.1` | PVC 10Gi | `/readyz`, `/livez` |
| `api` | Deployment | `homelab-api` (built from `api/`) | none | `GET /health` |
| `worker` | Deployment (Recreate) | `homelab-worker` (built from `worker/Dockerfile`) | none | none: a poll loop with no port |
| `web` | Deployment | `homelab-web` (built from the homelab-web repo) | none | TCP :3000 |
| `pushgateway` | Deployment | `prom/pushgateway:v1.11.5` | none (as in compose) | `/-/ready`, `/-/healthy` |
| `prometheus` | StatefulSet | `prom/prometheus:v3.16.0` | PVC 20Gi | `/-/ready`, `/-/healthy` |
| `grafana` | Deployment (Recreate) | `grafana/grafana:13.2.6` | PVC 2Gi, kept on uninstall | `/api/health` |
| `mlflow` | Deployment (Recreate) | `ghcr.io/mlflow/mlflow:v2.19.0` | PVC 10Gi, kept on uninstall | startup/readiness `/health` |
| `ingress` | Ingress (Traefik) | | | |

Choices that differ from compose:

- **Caddy becomes an Ingress.** Compose runs `caddy reverse-proxy --from :80 --to web:3000`.
  On k3s the bundled Traefik controller does that, so the chart renders an Ingress with no host
  (any Host header, like Caddy's `:80`) routing `/` to `web:3000`. Add hosts and `tls` in
  `ingress.hosts`/`ingress.tls`.
- **Images are pinned.** Compose floats qdrant, prometheus, pushgateway and grafana on
  `:latest`; the chart pins exact tags and `values.schema.json` rejects `latest`.
- **Readiness waits instead of `depends_on`.** api, worker and mlflow have a
  `wait-for-postgres` initContainer (`pg_isready`), the equivalent of compose's
  `condition: service_healthy`. Under Argo CD, sync waves order the rollout as well.
- **The web probe is TCP.** The home page server-renders `api /health/gpu`, which waits on the
  GPU PC's LLM backend. On the test cluster that took about 17 s, so `GET /` failed a 5 s
  readiness probe even though the pod was fine.
- **No root containers where the image allows it.** postgres (uid 70), redis (999),
  prometheus (65534) and grafana (472) run with `fsGroup` set, instead of compose's
  `user: "0"`. Every pod has `seccompProfile: RuntimeDefault`,
  `allowPrivilegeEscalation: false` and no service account token.
- **Service names are the compose names** (`api`, `postgres`, `prometheus`, ...), not
  release-prefixed, so the copied `prometheus.yml` targets (`api:8000`) and the Grafana
  datasource (`http://prometheus:9090`) work unchanged. One release per namespace.

## Secrets

The chart never renders a Secret and has no secret values in `values.yaml`. Every component
reads the existing Secret named by `secrets.existingSecret` (default `homelab-secrets`). Its
keys are the `.env` names, so on a real cluster:

```bash
kubectl create namespace homelab
kubectl -n homelab create secret generic homelab-secrets --from-env-file=.env
```

| Key | Used by | Required |
|-----|---------|----------|
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | postgres, api, worker, mlflow | yes |
| `API_KEY` | api, web (`HOMELAB_API_KEY`) | yes |
| `WORKER_API_KEY` | api, worker | yes |
| `GRAFANA_PASSWORD` | grafana | yes |
| `GRAFANA_USER`, `LLM_API_KEY`, `LLM_FALLBACK_API_KEY`, `REACTION_SERVE_KEY` | grafana, api | no (`optional: true`) |

`DATABASE_URL` is assembled in the pod with Kubernetes `$(VAR)` expansion, so the password
must be URL-safe. `bootstrap-env.sh` generates hex, which is.

Non-secret api settings (`LLM_BACKEND`, `LLM_URL`, `GPU_URL`, ...) are in `values.config` and
end up in the `homelab-config` ConfigMap.

## Config files from the repo

Helm's `.Files` can only read inside the chart directory, and symlinks are unreliable in a repo
that is also checked out on Windows. So the chart keeps **copies** under `homelab/files/`:

| Source (compose uses these) | Copy in the chart | Becomes |
|------------------------------|-------------------|---------|
| `prometheus/config/prometheus.yml` | `files/prometheus/prometheus.yml` | ConfigMap `prometheus-config` |
| `grafana/provisioning/datasources/*.yml` | `files/grafana/datasources/` | ConfigMap `grafana-datasources` |
| `grafana/provisioning/dashboards/default.yml` | `files/grafana/dashboard-providers/` | ConfigMap `grafana-dashboard-providers` |
| `grafana/dashboards/*.json` (4 dashboards) | `files/grafana/dashboards/` | ConfigMap `grafana-dashboards` |
| `postgres/*.sql` | `files/postgres/` | ConfigMap `postgres-initdb` at `/docker-entrypoint-initdb.d` |

The repo-root files stay the source of truth. After editing one, run
`bash charts/sync-chart-files.sh`. `bash charts/sync-chart-files.sh --check` (step 1 of
`test.sh`) fails if a copy has drifted. Pods roll when their config changes (checksum
annotations). `prometheus.configOverride` replaces `prometheus.yml` entirely if needed.

The init SQL runs only when the Postgres volume is empty, exactly like compose. Migrations on
an existing database still go through `scripts/migrate-all.sh`.

## Install

```bash
# 1. Images: api and worker from this repo, web from ../homelab-web. Push them to a registry
#    the cluster can pull from, and set the tags:
docker build -t <registry>/homelab-api:<tag> api
docker build -t <registry>/homelab-worker:<tag> -f worker/Dockerfile .
docker build -t <registry>/homelab-web:<tag> ../homelab-web

# 2. Secret (see above), then the chart
helm install homelab charts/homelab -n homelab \
  --set api.image.repository=<registry>/homelab-api --set api.image.tag=<tag> \
  --set worker.image.repository=<registry>/homelab-worker --set worker.image.tag=<tag> \
  --set web.image.repository=<registry>/homelab-web --set web.image.tag=<tag>
```

With GitOps, Argo CD does step 2 instead: see [argocd/](../argocd) and
[docs/GITOPS.md](../docs/GITOPS.md).

Turn components off with `--set <name>.enabled=false` (for example `mlflow`, `web`, `ingress`).
`ci/minimal-values.yaml` is the smallest useful set: api, postgres and redis.

## Tests

```bash
bash charts/test.sh        # ~30 s with warm image caches; needs only Docker
bash charts/test-k3d.sh    # creates, tests and deletes a k3d cluster named hs-gitops
```

`test.sh` checks, for four value sets (default, local, minimal, all-enabled):
`helm lint --strict`, then `helm template` piped through `kubeconform -strict` against the
Kubernetes 1.36.6 schemas, then the [helm-unittest](https://github.com/helm-unittest/helm-unittest)
suites in `homelab/tests/` (secret references, toggles, PVCs, config files, sync waves). Results
are in [docs/GITOPS.md](../docs/GITOPS.md#results).
