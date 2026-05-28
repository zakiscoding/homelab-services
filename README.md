# homelab-services

**iMac services stack** — API gateway, databases, reverse proxy, monitoring, MLflow. Proxies LLM and vision requests to the GPU node.

| | |
|--|--|
| **GitHub** | [github.com/zakiscoding/homelab-services](https://github.com/zakiscoding/homelab-services) (private) |
| **Runs on** | iMac (`Mos-iMac`, user `momo`) |
| **Docs** | [zakiscoding/homelab](https://github.com/zakiscoding/homelab) — architecture, setup log, runbooks |
| **1TB path** | `/Volumes/1tb drive/homelab-services` |

## Architecture

```text
Browser (Tailscale)
        │
        ├─ :3081 ──► homelab-web (Next.js)
        └─ :80   ──► Caddy ──► homelab-web
                │
                │  /api/homelab/* (BFF) + FastAPI :8000
                ▼
         Postgres / Redis / Qdrant / MLflow
                │
                │  Tailscale
                ▼
         GPU PC (zak_pc)
           Ollama :11434
           Reaction CNN :8100 (homelab-gpu)
```

## Docker: Colima (1TB drive)

We use **Colima**, not Docker Desktop. VM disk: `/Volumes/1tb drive/homelab-runtime/colima`.

```bash
brew install colima docker docker-compose
./scripts/setup-1tb.sh
source ./scripts/docker-1tb-env.sh
./scripts/start-stack.sh
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3081/
```

Uses `docker-compose` (hyphen) and `~/1tb-drive` symlink when paths have spaces.

## Services (Tailscale `100.91.113.98`)

| Service | Port | URL / notes |
|---------|------|-------------|
| **Web UI** (`homelab-web`) | **3081** | http://100.91.113.98:3081 — chat, RAG, reactions, status |
| Caddy → Web UI | 80 | http://100.91.113.98/ |
| FastAPI | 8000 | `/chat`, `/rag/*`, `/vision/*`, `/jobs` (`x-api-key`) |
| GPU worker API | 8000 | `/internal/worker/*` (`X-Worker-Key`) |
| Grafana | 3000 | http://100.91.113.98:3000 |
| Prometheus | 9090 | http://100.91.113.98:9090 |
| Pushgateway | 9091 | GPU metrics push target |
| Qdrant | 6333 | http://100.91.113.98:6333 |
| MLflow | 5001 | http://100.91.113.98:5001 |
| Redis | 6379 | Job wake (GPU worker; LAN/Tailscale only) |

Persistent data: `./data/` (gitignored).

## Migrations (after pull)

```bash
source ./scripts/docker-1tb-env.sh
./scripts/migrate-distributed-train.sh   # Phase 6 — train_plans / shards
./scripts/migrate-mlflow.sh              # mlflow database
./scripts/sync-grafana-provisioning.sh
./scripts/sync-prometheus-config.sh
docker-compose up -d --build
```

## Git

```bash
git pull
# edit, then:
git add . && git commit -m "..." && git push
```

Never commit `.env`. Create it once:

```bash
./scripts/bootstrap-env.sh          # iMac / Linux
# Windows laptop: .\scripts\bootstrap-env.ps1
```

## Web UI scripts

| Script | Purpose |
|--------|---------|
| `ensure-homelab-web.sh` | Clone sibling `homelab-web` repo |
| `render-caddyfile.sh` | LAN or public Caddy config |
| `sync-caddy-config.sh` | Colima-safe Caddyfile inject when `DOMAIN` set |
| `bootstrap-env.sh` | Generate `.env` with random secrets |
| `sync-grafana-provisioning.sh` | Copy dashboards into `data/grafana` |

Docs: [homelab web-ui.md](https://github.com/zakiscoding/homelab/blob/main/docs/services/web-ui.md) · [vision-reactions.md](https://github.com/zakiscoding/homelab/blob/main/docs/services/vision-reactions.md)
