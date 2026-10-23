# Homelab API reference

FastAPI app in `api/main.py` (title "Homelab API", version 0.8.0), served on the iMac at
`:8000` and fronted by Caddy / the `homelab-web` BFF. This reference is derived from the code
in `api/`; FastAPI's own `/docs`, `/redoc` and `/openapi.json` are also served (no auth).

22 application endpoints: 17 in `main.py`, 3 under `/rag`, 2 under `/vision`.

## Auth

| Header | Applies to | Checked against | Failure |
|--------|-----------|-----------------|---------|
| `x-api-key` | user routes: `/models`, `/chat`, `/stats`, `/system/status`, `/jobs*`, `/rag/*`, `/vision/*` | `API_KEY` | 401 `Invalid or missing API key`. If `API_KEY` is empty the check is skipped. |
| `X-Worker-Key` | `/internal/worker/*` | `WORKER_API_KEY` | 401 on mismatch; 503 `WORKER_API_KEY not configured` if unset (fails closed). |
| none | `/health`, `/health/gpu`, `/metrics` | | |

Worker routes also read `X-Worker-Id` (stored in `locked_by` and `worker_heartbeats`) and, for
the heartbeat, `X-Worker-Host`. They are identification only, not authorization: any holder of
the worker key can complete or fail any job.

## Job lifecycle

Postgres `jobs` is the single source of truth (`api/jobs.py`). Redis is only a wake signal.

```text
             claim (FOR UPDATE SKIP LOCKED, sets lease)
  pending ─────────────────────────────────────────────► running ──► succeeded
     ▲                                                      │
     │  transient failure or lease expiry, attempts left    │
     └──────────── attempt+1, scheduled_at = now+backoff ◄──┤
                                                            └──► failed
                       (non-transient, retry=false, or attempts exhausted)
```

| Type | Created by | Executed by | Claim route | Lease | Priority |
|------|-----------|-------------|-------------|-------|----------|
| `infer` | `POST /chat` | the API, inline | never claimed | none | 10 |
| `rag_query` | `POST /rag/query` | the API, inline | never claimed | none | 20 |
| `ingest` | `POST /rag/ingest` | iMac worker | `claim-imac` | 5 min | 30 |
| `train_prep` | `POST /jobs` with `distributed=true` | iMac worker | `claim-imac` | 5 min | 35 |
| `embed` | `POST /rag/ingest` (`auto_embed`) | iMac worker | `claim-imac` | 5 min | 40 |
| `train` | `POST /jobs` | GPU worker (QLoRA) or router worker | `claim` / `claim?routed=true` | 15 min | 50 |

Rules:

- **Ordering.** Lowest `priority`, then oldest `created_at`. Only rows with `scheduled_at <= NOW()` are eligible.
- **Concurrency.** At most one `train` job running at a time (across routed and non-routed), and at most one iMac job (`ingest`/`embed`/`train_prep`) at a time. `FOR UPDATE SKIP LOCKED` keeps two claimers off the same row. The one-at-a-time rule is a `NOT EXISTS` check rather than a lock, so it is not airtight under a simultaneous claim race.
- **Routed jobs.** A train job whose payload has a `route` key is claimed only via `POST /internal/worker/jobs/claim?routed=true` (the gpu-systems-lab router worker, which picks local RTX 5070 vs Modal). The default claim only returns jobs without `route`.
- **Distributed train.** A `train` job with `distributed=true` is not claimable until its `train_prep` job has written a ready `train_plans` row (`ready_at` set).
- **Leases.** Claiming sets `locked_by`, `started_at` (first claim only) and `lease_expires_at`. Nothing renews a lease (heartbeats do not), so a job running past its lease is requeued by the next sweep. Expired leases are swept on every claim of the same family and by `reset-stale`.
- **Retries.** `max_attempts` defaults to 3 (schema). A failure retries only when `retry` is true (default: `error_class == "transient"`), `error_class` is `transient`, and `attempt + 1 < max_attempts`. Backoff by current attempt: 30 s, 120 s, then 600 s. Lease expiry follows the same rule with `error_message = lease_expired` (or `lease_expired_max_attempts` when exhausted).
- **Wake queues.** New and requeued jobs `LPUSH` their id onto `homelab:jobs:wake:train` (train) or `homelab:jobs:wake:imac` (ingest/embed/train_prep), trimmed to 1000 entries. Workers `BLPOP` them to wake early. Disabled by `JOB_NOTIFY_ENABLED=false`. Losing a wake only delays a job until the next poll.
- **Flags** (`system_settings`): `training_paused = "true"` makes `POST /jobs` return 503; `allow_infer_during_train = "true"` lets `/chat` run while a train job is running.

## LLM backend selection

`api/llm.py`, chosen by `LLM_BACKEND`:

| Value | Backend | Endpoints used |
|-------|---------|----------------|
| `ollama` (default) | Ollama at `GPU_URL` (GPU PC, Windows boot) | `/api/generate`, `/api/tags` |
| `openai` | vLLM or any OpenAI-compatible server at `LLM_URL` (GPU PC, Ubuntu boot), `Authorization: Bearer $LLM_API_KEY` | `/chat/completions`, `/models` |
| `auto` | `openai` when `LLM_URL/models` answers 200 within 2 s, else `ollama`; re-probed at most every 30 s | |

With the openai backend a requested model the server does not serve falls back to
`LLM_DEFAULT_MODEL` (or the first served model), and responses report the model that actually
answered. `LLM_MAX_TOKENS` caps completions (default 1024).

## Shared shapes

**JobResponse**

```json
{
  "id": "uuid", "type": "train", "status": "pending|running|succeeded|failed",
  "locked_by": "worker-id|null", "error_message": "str|null", "error_class": "str|null",
  "created_at": "iso8601", "started_at": "iso8601|null", "finished_at": "iso8601|null",
  "priority": 50, "attempt": 0, "max_attempts": 3,
  "lease_expires_at": "iso8601|null", "scheduled_at": "iso8601"
}
```

Worker claim responses are JobResponse plus `"payload": {...}` (`{}` when empty).

---

## Health and metrics (no auth)

### `GET /health`
Liveness only. Response: `{"status": "ok"}`.

### `GET /health/gpu`
Probes the active LLM backend. Always 200.
Response: `{"gpu": "ok", "backend": "ollama|openai", "mode": "<LLM_BACKEND>", "models": [...]}`
or `{"gpu": "down", "backend", "mode", "error": "..."}`.

### `GET /metrics`
Prometheus text format. Refreshes gauges at most every `METRICS_REFRESH_SEC` (15 s) from
Postgres (`homelab_jobs_pending|running|failed_24h{type}`, `homelab_worker_last_seen_age_seconds`)
and Redis (`homelab_redis_requests_total`, `homelab_job_notify_*`), plus
`homelab_http_requests_total` / `homelab_http_request_duration_seconds` from middleware
(which skips `/metrics`, `/health`, `/docs`, `/redoc`, `/openapi.json`) and `homelab_vision_*`.

## User routes (`x-api-key`)

### `GET /models`
Lists models on the active backend.
Response: `{"models": [...], "homelab_models": [...], "default_chat_model": "...", "backend": "...", "mode": "..."}`.
`default_chat_model` prefers the first `homelab-*` model (LoRA published to Ollama), then the
backend default if served, else the first model. Errors: 502 if the backend call fails.

### `POST /chat`
Body (`ChatRequest`): `prompt` (str, required, min length 1), `model` (str, default `llama3.2:latest`).
Response (`ChatResponse`): `{"model", "response", "duration_ms", "job_id"}`.
Side effects: inserts an `infer` job and sets it `running` (no lease), calls the LLM backend,
inserts `chat_logs`, marks the job `succeeded`, `INCR homelab:requests:total` in Redis.
Errors: 503 `gpu_busy` when a train job is running and `allow_infer_during_train` is not set;
502 on LLM failure (the infer job is passed to `mark_job_failed` as transient, which puts it
back to `pending` with backoff while attempts remain; nothing claims infer jobs). If job
creation fails for a non-`RuntimeError` reason, chat runs without a job.

### `GET /stats`
Response: `{"requests_total": N}` from Redis `homelab:requests:total` (0 without Redis).

### `GET /system/status`
Read-only aggregate for the web UI. Response keys: `timestamp`, `control_plane`, `job_notify`
(`enabled`, wake queue depths), `jobs` (`pending`, `running`, `failed_last_24h`, `by_type`),
`queue_depth`, `workers` (heartbeats), `flags`, `recent_failures` (up to 10, last 24 h),
`gpu` (`reachable`, `ollama_up` — means "LLM backend up", `llm_backend`, `llm_mode`, `llm_url`,
`train_slot_free`), `rag` (`health`, `qdrant_ready`, `active_collection_version`,
`embedding_model_id`, `pending_chunks`). Makes sync HTTP calls to the LLM backend and Qdrant.
Errors: 500 on DB errors.

### `POST /jobs`
Queues a `train` job. Body (`TrainJobCreate`, optional, all fields optional):

| Field | Type | Goes to payload |
|-------|------|-----------------|
| `dataset_version`, `model_version`, `base_model` | str | as-is |
| `train_mode`, `dataset`, `architecture` | str | when non-empty |
| `epochs`, `batch_size`, `shard_count`, `grad_accum_steps` | int | when set |
| `learning_rate` | float | when set |
| `distributed`, `use_amp` | bool | when set |
| `route` | object | when non-empty; `route.command` required. Router spec: `command`, `vram_gb`, `local_minutes`, `gpus`, `max_usd`, `deadline_min` |

Response: JobResponse (`status: "pending"`).
Side effects: inserts a `train` job, wakes `homelab:jobs:wake:train`; with `distributed=true`
also inserts a `train_prep` job (payload copy + `parent_train_id`, `shard_count` default 8)
and wakes `homelab:jobs:wake:imac`.
Errors: 422 missing `route.command`; 503 `training_paused is true` (or `DATABASE_URL` unset); 500 other DB errors.

### `GET /jobs/{job_id}`
Response: JobResponse. Errors: 404 `Job not found`; 422 invalid UUID.

### `POST /rag/ingest`
Body (`IngestRequest`): `text` (str, required), `source_id` (str, optional), `auto_embed` (bool, default true).
Response (`IngestResponse`): `{"document_id", "ingest_job_id", "embed_job_id": "uuid|null", "duplicate": bool}`.
Side effects: upserts `rag_documents` by SHA-256 of `text`; always inserts an `ingest` job;
inserts an `embed` job when `auto_embed` and the document was not already ingested
(`duplicate=false`); both wake `homelab:jobs:wake:imac`. The iMac worker later chunks the text
into `rag_chunks` (400 chars, 50 overlap) and embeds up to 128 pending chunks per embed job into
the active Qdrant collection, then purges `rag:{version}:*` cache keys.

### `GET /rag/status`
Response: `{"active": {"name", "collection_version", "qdrant_collection_name", "embedding_model_id", "activated_at"}}`.
Errors: 503 `no_active_collection`.

### `POST /rag/query`
Body (`RagQueryRequest`): `query` (str, required), `model` (str, default `llama3.2:latest`), `top_k` (int 1–20, default 5).
Response (`RagQueryResponse`): `{"answer": "str|null", "reason": "no_context|null", "citations": [{"chunk_id", "score", "text"}], "model", "duration_ms", "job_id", "cached": bool}`.
Flow: Redis cache `rag:{collection_version}:{sha256(query)[:16]}` (hit returns `cached=true`;
key ignores `model` and `top_k`) → insert `rag_query` job and set it `running` (no lease) →
CPU MiniLM embedding → Qdrant search → drop hits below `RAG_SCORE_THRESHOLD` (0.35) → LLM
prompt with numbered context → job `succeeded`, response cached for 3600 s. With no qualifying
hits, `answer=null`, `reason="no_context"` (also cached).
Errors: 503 `rag_not_ready` (no active alias); 503 `qdrant_error: ...`; 502 on LLM failure
(job passed to `mark_job_failed` as transient).

### `GET /vision/status`
Calls the GPU reaction service (`GPU_VISION_URL`) `/health` and `/info`. Always 200:
`{"vision": "ok", "gpu_vision_url", ...info}` or `{"vision": "down", "gpu_vision_url", "error"}`.

### `POST /vision/reaction`
Body (`ReactionRequest`): `image_b64` (str, base64 JPEG/PNG, min length 32), `threshold` (float 0–1, optional).
Response (`ReactionResponse`): `{"label": "str|null", "confidence", "reason", "face_detected", "probabilities": {label: p}, "duration_ms", "bbox": {...}|null}`.
Side effects: forwards to GPU `/infer/reaction` with `X-Reaction-Key` (if `REACTION_SERVE_KEY`
set); no jobs rows. Errors: 502 on GPU error status or when unreachable.

## Worker routes (`X-Worker-Key`)

### `POST /internal/worker/heartbeat`
Headers: `X-Worker-Id` (default `unknown-worker`), `X-Worker-Host` (default: worker id).
Upserts `worker_heartbeats.last_seen_at`. Does not extend leases.
Response: `{"ok": "true", "worker_id": "..."}`.

### `POST /internal/worker/jobs/claim?routed=false|true`
Header `X-Worker-Id` (default `unknown-worker`). Records a heartbeat, sweeps expired train
leases, then claims one `train` job per the rules above (`routed=true`: only jobs with
`payload.route`; default: only jobs without). Sets `running`, `locked_by`, 15-min lease.
Response: JobResponse + `payload`, or **204** when nothing is claimable. Errors: 500 on DB errors.

### `POST /internal/worker/jobs/claim-imac`
Header `X-Worker-Id` (default `imac-worker`). Same as above for `ingest` / `embed` /
`train_prep`, one at a time, 5-min lease. Response: JobResponse + `payload`, or 204.

### `GET /internal/worker/train/{job_id}/plan`
Shard plan for a distributed train job: `{"train_job_id", "dataset", "dataset_version",
"total_samples", "shard_count", "val_sample_count", "seed", "meta", "ready_at",
"shards": [{"shard_index", "indices", "sample_count"}], "val_indices": [...]}`.
Errors: 404 `train plan not ready`.

### `POST /internal/worker/jobs/{job_id}/experiment`
Body (`ExperimentBody`, all optional): `run_id`, `dataset_version`, `model_version`,
`git_commit`, `hyperparams` (object), `output_path`.
Inserts an `experiments` row (`run_id` defaults to `exp-YYYYMMDD-xxxxxx`, must be unique).
Response: `{"id", "run_id", "job_id", "dataset_version", "model_version", "git_commit", "output_path", "created_at"}`.
Errors: 409 unless the job is `running` or `succeeded`; 500 on DB errors (e.g. duplicate `run_id`).

### `POST /internal/worker/jobs/{job_id}/succeeded`
`running` → `succeeded`; sets `finished_at`, clears lease and error. Response: JobResponse.
Errors: 409 if missing or not running (e.g. lease expired and the job was requeued).

### `POST /internal/worker/jobs/{job_id}/failed`
Body (`JobFailBody`): `error_message` (str, required; truncated to 2000), `error_class`
(str, default `transient`), `retry` (bool, optional; default `error_class == "transient"`).
Retries (back to `pending`, `attempt+1`, backoff, wake) or fails the job per the retry rule.
Response: JobResponse. Errors: 409 if missing or already `succeeded`/`failed`.

### `POST /internal/worker/jobs/reset-stale`
Sweeps expired leases (requeue or fail), then force-resets **every** running
train/ingest/embed/train_prep job to `pending` (no attempt bump), including a train job a GPU
worker may still be running. The iMac worker calls this on startup.
Response: `{"lease_expired_requeued": N, "forced_reset": M}`. Errors: 500 on DB errors.

## Environment variables

| Variable | Module | Default | Purpose |
|----------|--------|---------|---------|
| `DATABASE_URL` | main, jobs, rag_store, train_plan, metrics | `""` | Postgres DSN |
| `REDIS_URL` | main, rag_routes, job_notify, metrics | `redis://redis:6379/0` (`""` in metrics) | counter, RAG cache, wake queues |
| `API_KEY` | main, rag_routes, vision_routes | `""` (auth off) | user `x-api-key` |
| `WORKER_API_KEY` | main | `""` (worker routes 503) | `X-Worker-Key` |
| `LLM_BACKEND` | llm | `ollama` | `ollama` / `openai` / `auto` |
| `GPU_URL` | llm | `http://100.88.71.33:11434` | Ollama |
| `LLM_URL`, `LLM_API_KEY` | llm | `http://100.121.51.35:8000/v1`, `""` | OpenAI-compatible server |
| `LLM_DEFAULT_MODEL`, `LLM_MAX_TOKENS` | llm | `qwen2.5-1.5b-instruct`, `1024` | openai backend defaults |
| `QDRANT_URL` | rag_engine | `http://qdrant:6333` | vector store (status check uses `http://qdrant:6333` directly) |
| `EMBEDDING_MODEL_ID`, `RAG_SCORE_THRESHOLD` | rag_engine | `minilm-l6-v1`, `0.35` | RAG |
| `GPU_VISION_URL`, `REACTION_SERVE_KEY` | vision_routes | `http://100.88.71.33:8100`, `""` | reaction CNN proxy |
| `JOB_NOTIFY_ENABLED` | job_notify, worker | `true` | Redis wake queues |
| `METRICS_REFRESH_SEC` | metrics | `15` | gauge refresh interval |
