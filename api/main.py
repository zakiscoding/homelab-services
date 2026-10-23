"""Homelab API: the iMac FastAPI gateway for chat, RAG, vision and the GPU/iMac job queue.

Route groups:
  /health, /health/gpu, /metrics    no auth (liveness, LLM backend probe, Prometheus scrape)
  /models, /chat, /stats,           user routes, ``x-api-key`` header
  /system/status, /jobs, /jobs/{id}
  /rag/*                            rag_routes.router, ``x-api-key``
  /vision/*                         vision_routes.router, ``x-api-key``
  /internal/worker/*                worker routes, ``X-Worker-Key`` header

Auth model: user routes compare ``x-api-key`` with API_KEY; when API_KEY is empty the check
is skipped (open dev mode). Worker routes compare ``X-Worker-Key`` with WORKER_API_KEY and
fail closed (503) when it is unset. Workers identify themselves with ``X-Worker-Id`` /
``X-Worker-Host``, which only feed heartbeats and ``locked_by``.

Postgres (jobs.py) is the job source of truth. Redis holds the /chat request counter
(``homelab:requests:total``), the RAG answer cache and the worker wake queues
(job_notify.py); it never holds job state. Prometheus request metrics come from
metrics.prometheus_middleware.

Env vars read here: DATABASE_URL, REDIS_URL (default redis://redis:6379/0), API_KEY,
WORKER_API_KEY. Imported modules also read LLM_BACKEND, GPU_URL, LLM_URL, LLM_API_KEY,
LLM_DEFAULT_MODEL, LLM_MAX_TOKENS (llm.py); QDRANT_URL, EMBEDDING_MODEL_ID,
RAG_SCORE_THRESHOLD (rag_engine.py); GPU_VISION_URL, REACTION_SERVE_KEY (vision_routes.py);
JOB_NOTIFY_ENABLED (job_notify.py); METRICS_REFRESH_SEC (metrics.py).
"""

import os
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import psycopg
import redis
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

import llm

from jobs import (
    claim_imac_job,
    claim_train_job,
    create_experiment_for_job,
    create_infer_job,
    create_train_job,
    expire_stale_leases,
    get_job,
    get_system_status,
    mark_job_failed,
    mark_job_running,
    mark_job_succeeded,
    record_worker_heartbeat,
    renew_lease,
    reset_stale_running_to_pending,
)
from metrics import metrics_body, prometheus_middleware
from train_plan import get_train_plan
from rag_routes import router as rag_router
from vision_routes import router as vision_router

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
API_KEY = os.environ.get("API_KEY", "")
WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")

_redis: redis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Open the shared Redis client (if REDIS_URL is set) for the app's lifetime."""
    global _redis
    if REDIS_URL:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    yield
    if _redis:
        _redis.close()


app = FastAPI(title="Homelab API", version="0.8.0", lifespan=lifespan)
app.include_router(rag_router)
app.include_router(vision_router)
prometheus_middleware(app)


@app.get("/metrics")
def prometheus_metrics() -> Response:
    """Prometheus scrape target (internal / LAN, no API key).

    Refreshes the Postgres/Redis-backed gauges at most every METRICS_REFRESH_SEC, then returns
    the text exposition format.
    """
    return Response(content=metrics_body(), media_type="text/plain; version=0.0.4; charset=utf-8")


def require_api_key(x_api_key: str | None) -> None:
    """Raise 401 unless ``x-api-key`` matches API_KEY; a no-op when API_KEY is empty."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_worker_key(x_worker_key: str | None) -> None:
    """Guard for /internal/worker/*: 503 if WORKER_API_KEY is unset, 401 if ``X-Worker-Key`` differs."""
    if not WORKER_API_KEY:
        raise HTTPException(status_code=503, detail="WORKER_API_KEY not configured")
    if x_worker_key != WORKER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing worker API key")


def log_chat(prompt: str, response: str, model: str, duration_ms: int, job_id: str | None = None) -> None:
    """Best-effort INSERT of a chat exchange into ``chat_logs``.

    Skipped when DATABASE_URL is unset; any DB error is swallowed so logging never fails a request.
    """
    if not DATABASE_URL:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_logs (prompt, response, model, duration_ms, job_id)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (prompt, response, model, duration_ms, job_id),
                )
            conn.commit()
    except Exception:
        pass


class ChatRequest(BaseModel):
    """Body of POST /chat. ``model`` may be remapped by the openai backend (see llm.generate)."""
    prompt: str = Field(..., min_length=1)
    model: str = "llama3.2:latest"


class ChatResponse(BaseModel):
    """POST /chat result; ``model`` is the model that answered, ``job_id`` the infer job (if any)."""
    model: str
    response: str
    duration_ms: int
    job_id: str | None = None


class JobResponse(BaseModel):
    """Public view of a ``jobs`` row; ISO 8601 timestamps, no payload (worker claim routes add it)."""
    id: str
    type: str
    status: str
    locked_by: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    priority: int | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    lease_expires_at: str | None = None
    scheduled_at: str | None = None
    error_class: str | None = None


class JobFailBody(BaseModel):
    """Body of POST /internal/worker/jobs/{id}/failed.

    ``error_class`` is free text; only ``"transient"`` is eligible for retry. ``retry`` defaults to
    ``error_class == "transient"``; ``retry=true`` with a non-transient class still fails the job.
    """
    error_message: str = Field(..., min_length=1)
    error_class: str = "transient"
    retry: bool | None = None


class TrainJobCreate(BaseModel):
    """Body of POST /jobs (all fields optional).

    ``dataset_version``, ``model_version`` and ``base_model`` go into the payload as-is; the other
    fields are copied into the payload only when set. ``distributed=true`` also queues a train_prep
    job and holds the train job until its shard plan is ready. ``route`` marks the job for the
    gpu-systems-lab router worker.
    """
    dataset_version: str | None = None
    model_version: str | None = None
    base_model: str | None = None
    train_mode: str | None = None
    dataset: str | None = None
    architecture: str | None = None
    epochs: int | None = None
    batch_size: int | None = None
    learning_rate: float | None = None
    distributed: bool | None = None
    shard_count: int | None = None
    grad_accum_steps: int | None = None
    use_amp: bool | None = None
    # gpu-systems-lab router job: {"command", "vram_gb", "local_minutes", "gpus",
    # "max_usd", "deadline_min"}. Claimed only by the router worker, which runs it on the
    # local RTX 5070 or a Modal GPU.
    route: dict[str, Any] | None = None


class ExperimentBody(BaseModel):
    """Body of POST /internal/worker/jobs/{id}/experiment; ``run_id`` defaults to ``exp-YYYYMMDD-xxxxxx``."""
    run_id: str | None = None
    dataset_version: str | None = None
    model_version: str | None = None
    git_commit: str | None = None
    hyperparams: dict[str, Any] | None = None
    output_path: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe (no auth, no dependencies checked)."""
    return {"status": "ok"}


@app.get("/models")
async def list_models(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Models on the LLM backend (Ollama tags, or vLLM served models).

    With Ollama, homelab-* models (publish-lora-to-ollama) are preferred as the default.
    Auth: ``x-api-key``. Makes an HTTP call to the active backend; any failure -> 502.
    """
    require_api_key(x_api_key)
    try:
        names = await llm.list_models()
        homelab = [n for n in names if n.startswith("homelab-")]
        if homelab:
            default = homelab[0]
        elif llm.default_model() in names or not names:
            default = llm.default_model()
        else:
            default = names[0]
        return {"models": names, "homelab_models": homelab, "default_chat_model": default, "backend": llm.active_backend(), "mode": llm.MODE}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/health/gpu")
async def health_gpu() -> dict[str, Any]:
    """Probe the active LLM backend (no auth).

    Always 200: ``{"gpu": "ok", "models": [...]}`` when the backend lists models, else
    ``{"gpu": "down", "error": ...}``. Also reports ``backend`` and ``mode`` (LLM_BACKEND).
    """
    try:
        return {"gpu": "ok", "backend": llm.active_backend(), "mode": llm.MODE, "models": await llm.list_models()}
    except Exception as e:
        return {"gpu": "down", "backend": llm.active_backend(), "mode": llm.MODE, "error": str(e)}


@app.get("/system/status")
def system_status(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Aggregate control-plane status for the web UI (jobs.get_system_status).

    Auth: ``x-api-key``. Read-only: job counts, recent failures, worker heartbeats, flags, Redis
    wake-queue depth, LLM backend ping and Qdrant readiness. Any exception -> 500.
    """
    require_api_key(x_api_key)
    try:
        return get_system_status(llm.backend_url())
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    x_api_key: str | None = Header(default=None),
) -> ChatResponse:
    """Single-turn chat completion on the active LLM backend, tracked as an ``infer`` job.

    Auth: ``x-api-key``.
    Side effects: creates an ``infer`` jobs row and moves it straight to ``running`` (no lease, no
    worker; the API itself is the executor), calls llm.generate (Ollama or vLLM over Tailscale),
    logs the exchange to ``chat_logs``, marks the job ``succeeded`` and INCRs
    ``homelab:requests:total`` in Redis.
    Errors: 503 ``gpu_busy`` when a train job is running and ``allow_infer_during_train`` is not
    "true"; 502 on any LLM failure (the infer job goes through mark_job_failed as transient, which
    puts it back to ``pending`` with backoff while attempts remain; nothing ever claims infer
    jobs, so the row is bookkeeping only). If job creation fails for a non-RuntimeError reason the
    chat still runs without a job; other RuntimeErrors (e.g. DATABASE_URL unset) surface as 500.
    """
    require_api_key(x_api_key)
    infer_job = None
    try:
        infer_job = create_infer_job()
    except RuntimeError as e:
        if "gpu_busy" in str(e):
            raise HTTPException(status_code=503, detail="gpu_busy") from e
        raise
    except Exception:
        infer_job = None

    job_id = infer_job["id"] if infer_job else None
    if job_id:
        try:
            with psycopg.connect(DATABASE_URL) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE jobs SET status = 'running', started_at = NOW()
                        WHERE id = %s AND status = 'pending'
                        """,
                        (job_id,),
                    )
                conn.commit()
        except Exception:
            pass

    started = time.perf_counter()
    try:
        try:
            text, model_used = await llm.generate(body.model, body.prompt)
        except llm.LLMError as e:
            if job_id:
                mark_job_failed(UUID(job_id), str(e), error_class="transient", retry=False)
            raise HTTPException(status_code=502, detail=str(e)) from e
        duration_ms = int((time.perf_counter() - started) * 1000)
        log_chat(body.prompt, text, model_used, duration_ms, job_id)
        if job_id:
            mark_job_succeeded(UUID(job_id))
        if _redis:
            _redis.incr("homelab:requests:total")
        return ChatResponse(model=model_used, response=text, duration_ms=duration_ms, job_id=job_id)
    except HTTPException:
        raise
    except Exception as e:
        if job_id:
            mark_job_failed(UUID(job_id), str(e), error_class="transient", retry=False)
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/stats")
def stats(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Return ``{"requests_total": N}`` from the Redis counter (0 without Redis). Auth: ``x-api-key``."""
    require_api_key(x_api_key)
    total = int(_redis.get("homelab:requests:total") or 0) if _redis else 0
    return {"requests_total": total}


@app.post("/jobs", response_model=JobResponse)
def jobs_create_train(
    body: TrainJobCreate | None = None,
    x_api_key: str | None = Header(default=None),
) -> JobResponse:
    """Queue a ``train`` job (POST /jobs).

    Auth: ``x-api-key``. Builds the payload from TrainJobCreate and calls jobs.create_train_job,
    which INSERTs a pending train row (priority 50), LPUSHes a wake to
    ``homelab:jobs:wake:train`` and, for ``distributed=true``, also queues a ``train_prep`` job.
    Errors: 422 if ``route`` is given without a non-empty ``route.command``; 503 when
    ``training_paused`` is "true" (or DATABASE_URL is unset); 500 on other DB errors.
    """
    require_api_key(x_api_key)
    b = body or TrainJobCreate()
    hyper: dict[str, Any] = {}
    if b.train_mode:
        hyper["train_mode"] = b.train_mode
    if b.dataset:
        hyper["dataset"] = b.dataset
    if b.architecture:
        hyper["architecture"] = b.architecture
    if b.epochs is not None:
        hyper["epochs"] = b.epochs
    if b.batch_size is not None:
        hyper["batch_size"] = b.batch_size
    if b.learning_rate is not None:
        hyper["learning_rate"] = b.learning_rate
    if b.distributed is not None:
        hyper["distributed"] = b.distributed
    if b.shard_count is not None:
        hyper["shard_count"] = b.shard_count
    if b.grad_accum_steps is not None:
        hyper["grad_accum_steps"] = b.grad_accum_steps
    if b.use_amp is not None:
        hyper["use_amp"] = b.use_amp
    if b.route:
        if not str(b.route.get("command", "")).strip():
            raise HTTPException(status_code=422, detail="route.command is required")
        hyper["route"] = b.route
    try:
        job = create_train_job(
            dataset_version=b.dataset_version,
            model_version=b.model_version,
            base_model=b.base_model,
            hyperparams=hyper or None,
        )
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return JobResponse(**job)


@app.get("/jobs/{job_id}", response_model=JobResponse)
def jobs_get(job_id: UUID, x_api_key: str | None = Header(default=None)) -> JobResponse:
    """Fetch one job by id. Auth: ``x-api-key``. 404 if it does not exist; 422 for a malformed UUID."""
    require_api_key(x_api_key)
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobResponse(**job)


@app.post("/internal/worker/heartbeat")
def worker_heartbeat(
    x_worker_key: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None, alias="X-Worker-Id"),
    x_worker_host: str | None = Header(default=None, alias="X-Worker-Host"),
) -> dict[str, Any]:
    """Upsert ``worker_heartbeats`` for ``X-Worker-Id`` (default "unknown-worker").

    Auth: ``X-Worker-Key``. ``X-Worker-Host`` is stored as host (defaults to the worker id).
    Heartbeats feed /system/status and the Prometheus worker-age gauge; they do not extend leases.
    """
    require_worker_key(x_worker_key)
    worker_id = x_worker_id or "unknown-worker"
    record_worker_heartbeat(worker_id, x_worker_host)
    return {"ok": "true", "worker_id": worker_id}


@app.get("/internal/worker/train/{job_id}/plan")
def worker_get_train_plan(
    job_id: UUID,
    x_worker_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Return the shard plan for a distributed train job (train_plan.get_train_plan).

    Auth: ``X-Worker-Key``. 404 ``train plan not ready`` until the train_prep job has written it.
    """
    require_worker_key(x_worker_key)
    plan = get_train_plan(job_id)
    if not plan:
        raise HTTPException(status_code=404, detail="train plan not ready")
    return plan


@app.post("/internal/worker/jobs/claim-imac", response_model=None)
def worker_claim_imac(
    x_worker_key: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None, alias="X-Worker-Id"),
):
    """Claim the next iMac job (ingest, embed or train_prep) for ``X-Worker-Id``.

    Auth: ``X-Worker-Key``. Records a heartbeat, expires stale iMac leases, then claims at most one
    job (only if no iMac job is running) with a 5-minute lease. Returns JobResponse fields plus
    ``payload``, or 204 when nothing is claimable; 500 on DB errors.
    """
    require_worker_key(x_worker_key)
    worker_id = x_worker_id or "imac-worker"
    record_worker_heartbeat(worker_id)
    try:
        job = claim_imac_job(worker_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not job:
        return Response(status_code=204)
    base = JobResponse(**{k: job[k] for k in JobResponse.model_fields if k in job}).model_dump()
    base["payload"] = job.get("payload") or {}
    return base


@app.post("/internal/worker/jobs/claim", response_model=None)
def worker_claim_train(
    routed: bool = False,
    x_worker_key: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None, alias="X-Worker-Id"),
):
    """Claim the next pending ``train`` job for ``X-Worker-Id``.

    routed=true: only jobs with a route spec (router worker); default: only jobs without.
    Auth: ``X-Worker-Key``. Records a heartbeat, expires stale train leases, then claims one job
    with a 15-minute lease, only when no train job is running (see jobs.claim_train_job).
    Returns JobResponse fields plus ``payload``, 204 when nothing is claimable, 500 on DB errors.
    """
    require_worker_key(x_worker_key)
    worker_id = x_worker_id or "unknown-worker"
    record_worker_heartbeat(worker_id)
    try:
        job = claim_train_job(worker_id, routed=routed)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not job:
        return Response(status_code=204)
    base = JobResponse(**{k: job[k] for k in JobResponse.model_fields if k in job}).model_dump()
    base["payload"] = job.get("payload") or {}
    return base


@app.post("/internal/worker/jobs/{job_id}/experiment")
def worker_record_experiment(
    job_id: UUID,
    body: ExperimentBody,
    x_worker_key: str | None = Header(default=None),
) -> dict[str, Any]:
    """Record an ``experiments`` row for a train job.

    Auth: ``X-Worker-Key``. 409 unless the job exists and is ``running`` or ``succeeded``; 500 on
    DB errors (``run_id`` is UNIQUE, so a reused run_id fails here). Returns the inserted row.
    """
    require_worker_key(x_worker_key)
    job = get_job(job_id)
    if not job or job["status"] not in ("running", "succeeded"):
        raise HTTPException(status_code=409, detail="Job not running or not found")
    try:
        return create_experiment_for_job(
            job_id,
            run_id=body.run_id,
            dataset_version=body.dataset_version,
            model_version=body.model_version,
            git_commit=body.git_commit,
            hyperparams=body.hyperparams,
            output_path=body.output_path,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/internal/worker/jobs/{job_id}/succeeded", response_model=JobResponse)
def worker_job_succeeded(
    job_id: UUID,
    x_worker_key: str | None = Header(default=None),
) -> JobResponse:
    """Transition a ``running`` job to ``succeeded`` and clear its lease.

    Auth: ``X-Worker-Key``. 409 if the job is missing or not running (e.g. its lease already
    expired and it was requeued). Ownership (``locked_by``) is not checked.
    """
    require_worker_key(x_worker_key)
    job = mark_job_succeeded(job_id)
    if not job:
        raise HTTPException(status_code=409, detail="Job not running or not found")
    return JobResponse(**job)


@app.post("/internal/worker/jobs/{job_id}/failed", response_model=JobResponse)
def worker_job_failed(
    job_id: UUID,
    body: JobFailBody,
    x_worker_key: str | None = Header(default=None),
) -> JobResponse:
    """Report a job failure; the job is retried or failed (jobs.mark_job_failed).

    Auth: ``X-Worker-Key``. A ``transient`` failure with attempts left goes back to ``pending``
    with backoff (30s/120s/600s) and a wake notification; otherwise the job becomes ``failed``.
    409 if the job is missing or not ``running``/``pending``.
    """
    require_worker_key(x_worker_key)
    job = mark_job_failed(
        job_id,
        body.error_message,
        error_class=body.error_class,
        retry=body.retry,
    )
    if not job:
        raise HTTPException(status_code=409, detail="Job not found or not updatable")
    return JobResponse(**job)


@app.post("/internal/worker/jobs/reset-stale")
def worker_reset_stale(
    all_workers: bool = False,
    x_worker_key: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None, alias="X-Worker-Id"),
) -> dict[str, int]:
    """Requeue jobs whose lease expired, then force-reset the *caller's* orphaned running jobs.

    Auth: ``X-Worker-Key``. Called by workers on startup. ``lease_expired_requeued`` counts
    expired-lease jobs requeued or failed plus abandoned inline jobs closed (expire_stale_leases);
    ``forced_reset`` counts running jobs held by ``X-Worker-Id`` put back to ``pending``
    (a restarted worker can't still be running them). Jobs held by other workers are untouched,
    so one worker restarting no longer requeues another worker's live training job.
    ``?all_workers=true`` (operator only) resets every running job. Without ``X-Worker-Id``
    nothing is force-reset. 500 on DB errors.
    """
    require_worker_key(x_worker_key)
    try:
        expired = expire_stale_leases()
        if all_workers:
            forced = reset_stale_running_to_pending(None)
        elif x_worker_id:
            forced = reset_stale_running_to_pending(x_worker_id)
        else:
            forced = 0
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"lease_expired_requeued": expired, "forced_reset": forced}


@app.post("/internal/worker/jobs/{job_id}/renew", response_model=JobResponse)
def worker_renew_lease(
    job_id: UUID,
    x_worker_key: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None, alias="X-Worker-Id"),
) -> JobResponse:
    """Extend the lease on a running job this worker holds (keep-alive for long jobs).

    Auth: ``X-Worker-Key``; ownership by ``X-Worker-Id`` (must match ``locked_by``). Workers call
    it every few minutes while a job runs so lease expiry doesn't requeue live work. 409 if the
    job isn't running or another worker holds it: the caller should stop, because its job was
    requeued or taken over.
    """
    require_worker_key(x_worker_key)
    job = renew_lease(job_id, x_worker_id or "unknown-worker")
    if not job:
        raise HTTPException(status_code=409, detail="Job not running or not held by this worker")
    return JobResponse(**job)
