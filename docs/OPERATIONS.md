# Operations runbook

[homelab](https://github.com/zakiscoding/homelab) (docs hub) · **homelab-services** · [homelab-web](https://github.com/zakiscoding/homelab-web) · [homelab-gpu](https://github.com/zakiscoding/homelab-gpu) · [gpu-systems-lab](https://github.com/zakiscoding/gpu-systems-lab)

**How to start, update, migrate, back up and fix the iMac stack.** Back to [README](../README.md).

All commands run on the iMac (`momo@100.91.113.98`) from the repo root,
`/Volumes/1tb drive/homelab-services`. Docker runs in Colima, not Docker Desktop. The Colima VM
and Docker config live on the 1TB drive (`/Volumes/1tb drive/homelab-runtime/`), and the stack
uses `docker-compose` (hyphen). Paths with spaces are also reachable through the `~/1tb-drive`
symlink.

## Shell setup (every new shell)

```bash
cd "/Volumes/1tb drive/homelab-services"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"   # needed over non-interactive ssh
source ./scripts/docker-1tb-env.sh                      # COLIMA_HOME, DOCKER_CONFIG, docker context
```

Non-interactive SSH sessions (for example `ssh momo@100.91.113.98 'docker ps'`) do not load the
shell profile, so `docker`, `docker-compose` and `colima` are not on PATH until you add
`/opt/homebrew/bin`. Without `docker-1tb-env.sh`, Colima looks in `~/.colima` and reports no VM.

## Cold start

After an iMac reboot, or whenever the stack is down:

```bash
./scripts/start-stack.sh
```

`start-stack.sh` does the following:

1. Sources `docker-1tb-env.sh`.
2. Starts Colima if it is not running (`--cpu 4 --memory 6 --disk 80 --mount-type virtiofs`, mounting the 1TB drive writable).
3. Renders the Caddyfile, then runs `docker-compose pull` and `docker-compose up -d --build`.
4. Syncs the Prometheus config and Grafana provisioning into the running containers.
5. Syncs the Caddy config when `DOMAIN` is set in `.env`.
6. Checks the web UI and API `/health`.

Then run [health checks](#health-checks).

First-time setup on a new drive also needs `./scripts/ensure-homelab-web.sh`,
`./scripts/bootstrap-env.sh` and `./scripts/setup-1tb.sh`. See the README
[Quickstart](../README.md#quickstart).

## Deploy an update

```bash
git pull
docker-compose up -d --build api imac-worker     # rebuild only what changed
bash scripts/migrate-all.sh                      # if postgres/*.sql changed
```

| Changed | Action |
|---------|--------|
| `api/` | `docker-compose up -d --build api imac-worker` (the worker image copies some API modules) |
| `worker/` | `docker-compose up -d --build imac-worker` |
| `../homelab-web` | `git -C ../homelab-web pull && docker-compose up -d --build web` |
| `postgres/*.sql` | `bash scripts/migrate-all.sh` |
| `grafana/` | `./scripts/sync-grafana-provisioning.sh` |
| `prometheus/config/` | `./scripts/sync-prometheus-config.sh` |
| `docker-compose.yml` or `.env` | `docker-compose up -d` (recreates changed services) |

Prometheus and Grafana config is copied into the containers by the sync scripts, not
bind-mounted, because Colima's virtiofs breaks single-file bind mounts. Editing the files in git
has no effect until you run the sync script.

## Migrations

`postgres/init.sql` runs only when the Postgres data directory is first created. Every later
schema change is a numbered migration (`002_jobs.sql` to `007_mlflow.sql`). All migrations are
idempotent, so re-running them is safe.

```bash
bash scripts/migrate-all.sh
```

`migrate-all.sh`:

1. Refuses to run unless `homelab-services-postgres-1` is running.
2. Writes `backups/homelab-pre-migrate-<timestamp>.sql` with `pg_dump` and aborts if the file is empty.
3. Applies `postgres/0*.sql` in order with `ON_ERROR_STOP=1`. For `007_mlflow.sql` an "already exists" error is expected and ignored.
4. Prints the `jobs` columns and public tables, plus the exact restore command for the backup it took.

The older per-phase scripts (`migrate-jobs.sh`, `migrate-phase0b.sh`, `migrate-phase1.sh`,
`migrate-phase2.sh`, `migrate-distributed-train.sh`, `migrate-mlflow.sh`) each apply one file.
Prefer `migrate-all.sh`.

## Backup and restore

```bash
./scripts/backup-postgres.sh       # backups/homelab-YYYYMMDD-HHMM.sql; deletes dumps older than 14 days
```

Run it from cron or launchd for regular backups. `migrate-all.sh` also takes its own
`homelab-pre-migrate-*` backup, which the 14-day cleanup does not delete.

Restore into the running database:

```bash
docker exec -i homelab-services-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < backups/homelab-YYYYMMDD-HHMM.sql
```

The dump is a plain `pg_dump` of the main database, not the `mlflow` database. For a clean
restore, stop writers first (`docker-compose stop api imac-worker`), restore into an empty
database, then start them again. Qdrant vectors (`data/qdrant`) and MLflow artifacts
(`data/mlflow`) are not in the dump. Back up `data/` on the 1TB drive to keep them.

## Health checks

```bash
docker-compose ps                                      # every service Up; postgres (healthy)
curl -s http://localhost:8000/health                   # {"status":"ok"}
curl -s http://localhost:8000/health/gpu               # {"gpu":"ok","backend":"openai|ollama",...}
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:3081/   # 200
curl -s http://localhost:9090/-/ready                  # Prometheus
curl -s http://localhost:3000/api/health               # Grafana
curl -s http://localhost:6333/readyz                   # Qdrant
curl -s -H "x-api-key: <API_KEY>" http://localhost:8000/system/status  # jobs, workers, flags, LLM backend, RAG
```

Grafana's **Homelab Stack** and **Homelab ML Platform** dashboards show queue depth, failures
and worker last-seen age. The **GPU node** dashboard reads the Ubuntu node's Prometheus and is
empty while the GPU PC runs Windows.

## Common failures

| Symptom | Cause | Fix |
|---------|-------|-----|
| Every service unreachable; `docker ps` says it cannot connect to the daemon | Colima stopped (iMac reboot or sleep) | `source ./scripts/docker-1tb-env.sh && ./scripts/start-stack.sh` |
| `colima status` says not running even though the stack was up | `docker-1tb-env.sh` not sourced, so Colima looks in `~/.colima` | Source `docker-1tb-env.sh` first |
| `docker: command not found` over SSH | Non-interactive SSH does not load the profile | `export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"` |
| `api` or `imac-worker` in a restart loop after `git pull` | Container still runs an old image built from previous code | `docker-compose up -d --build api imac-worker`, then `docker-compose logs --tail=100 api` |
| API errors such as `column ... does not exist`, or `/jobs` returns 500 | Database is behind the code's migrations | `bash scripts/migrate-all.sh` |
| iMac worker exits at startup with a connection error | Worker started before the API was ready (cold start race) | Fixed: the worker now waits up to 180 s for `/health`. If it still fails, check `docker-compose logs api` |
| Chat is slow or fails right after the GPU PC reboots | `LLM_BACKEND=auto` caches its probe for 30 s | Wait 30 s. `/health/gpu` shows which backend is active |
| `/health/gpu` reports `backend: ollama` while you expect vLLM | GPU PC booted into Windows, or vLLM down on Ubuntu | Expected fallback. To use vLLM, boot Ubuntu and check `curl http://100.121.51.35:8000/v1/models` with the key |
| `/health/gpu` reports `gpu: down` | Neither vLLM nor Ollama reachable (GPU PC off, Tailscale down) | Check `tailscale status` and start the GPU PC |
| Grafana dashboards missing or datasource errors | Provisioning not synced into the container | `./scripts/sync-grafana-provisioning.sh` |
| Prometheus targets missing | Config not synced | `./scripts/sync-prometheus-config.sh` |
| `web` build fails: build context not found | `../homelab-web` not cloned | `./scripts/ensure-homelab-web.sh` |
| Jobs stuck `running` after a worker crash | Lease not yet expired | Wait for the lease (5 min iMac, 15 min train) or have the worker restart, which calls `reset-stale` for its own jobs. See [JOBS.md](JOBS.md) |
| Jobs stay `pending` | No worker of that family is polling, `training_paused` is set, or a distributed job's plan is not ready | Check `/system/status` workers and `docker-compose logs imac-worker`. See [JOBS.md](JOBS.md#who-claims-what) |

## Useful commands

```bash
docker-compose logs -f --tail=100 api imac-worker
docker-compose restart api
docker exec -it homelab-services-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
# queue snapshot
docker exec homelab-services-postgres-1 sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select type,status,count(*) from jobs group by 1,2 order by 1,2"'
```
