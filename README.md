# homelab-services

**iMac services stack** — API gateway, databases, reverse proxy, monitoring. Proxies LLM requests to the GPU node (Ollama).

| | |
|--|--|
| **GitHub** | [github.com/zakiscoding/homelab-services](https://github.com/zakiscoding/homelab-services) (private) |
| **Runs on** | iMac (`Mos-iMac`, user `momo`) |
| **Docs** | [zakiscoding/homelab](https://github.com/zakiscoding/homelab) — architecture, setup log, runbooks |
| **1TB path** | `/Volumes/1tb drive/homelab-services` |

## Architecture

```text
Client / controller
        │
        ▼
   iMac (this repo)
   Caddy :80/:443 → Web UI → FastAPI (internal) → Postgres / Redis
        │
        │  Tailscale
        ▼
   GPU PC (zak_pc) — Ollama :11434
   (training = separate homelab-gpu repo later)
```

## Docker: Colima (1TB drive)

We use **Colima**, not Docker Desktop. VM disk: `/Volumes/1tb drive/colima`.

```bash
brew install colima docker docker-compose
./scripts/setup-1tb.sh
./scripts/start-stack.sh
curl http://localhost/health
```

Uses `docker-compose` (hyphen) and `~/1tb-drive` symlink when paths have spaces.

## Services (verified 2026-05-21)

| Service | Port | Tailscale (`100.91.113.98`) |
|---------|------|-----------------------------|
| Caddy → Web UI | 80, 443 | `http://100.91.113.98/` — public HTTPS when `DOMAIN` set ([homelab docs](https://github.com/zakiscoding/homelab/blob/main/docs/deploy/public-internet.md)) |
| FastAPI | internal 8000 | `/chat`, `/jobs` (header `x-api-key`) |
| GPU worker API | internal | `/internal/worker/*` (header `X-Worker-Key`) — [homelab docs PHASE-0A](https://github.com/zakiscoding/homelab/blob/main/docs/PHASE-0A.md) |
| PostgreSQL | internal | Chat logs in `data/postgres` |
| Redis | internal | Counters |
| Qdrant | 6333 | `http://100.91.113.98:6333/` |
| Prometheus | 9090 | `http://100.91.113.98:9090/-/healthy` |
| Grafana | 3000 | `http://100.91.113.98:3000` |
| **Web UI** (`homelab-web`) | **3080** | `http://100.91.113.98:3080` — chat, RAG, status |

Persistent data: `./data/` (gitignored).

## Git

```bash
git pull
# edit, then:
git add . && git commit -m "..." && git push
```

Never commit `.env`.
