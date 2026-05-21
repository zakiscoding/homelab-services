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
   Caddy :80 → FastAPI → Postgres / Redis
        │
        │  Tailscale
        ▼
   GPU PC (zak_pc) — Ollama :11434
   (training = separate homelab-gpu repo later)
```

## Quick start

```bash
cd "/Volumes/1tb drive/homelab-services"
cp .env.example .env          # edit GPU_URL, passwords, API_KEY
mkdir -p data/{postgres,redis,qdrant,prometheus,grafana,caddy,caddy-config}
docker compose up -d --build
curl http://localhost/health
```

## Services

| Service | Port | Notes |
|---------|------|-------|
| Caddy | 80, 443 | Reverse proxy → API |
| API | internal 8000 | FastAPI |
| PostgreSQL | internal | Chat logs |
| Redis | internal | Counters |
| Qdrant | 6333 | Vectors / RAG |
| Prometheus | 9090 | Metrics |
| Grafana | 3000 | Dashboards |

Persistent data: `./data/` (gitignored).

## Git

```bash
git pull
# edit, then:
git add . && git commit -m "..." && git push
```

Never commit `.env`.
