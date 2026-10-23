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
    """Prometheus scrape target (internal / LAN — no API key)."""
    return Response(content=metrics_body(), media_type="text/plain; version=0.0.4; charset=utf-8")


def require_api_key(x_api_key: str | None) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_worker_key(x_worker_key: str | None) -> None:
    if not WORKER_API_KEY:
        raise HTTPException(status_code=503, detail="WORKER_API_KEY not configured")
    if x_worker_key != WORKER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing worker API key")


def log_chat(prompt: str, response: str, model: str, duration_ms: int, job_id: str | None = None) -> None:
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
    prompt: str = Field(..., min_length=1)
    model: str = "llama3.2:latest"


class ChatResponse(BaseModel):
    model: str
    response: str
    duration_ms: int
    job_id: str | None = None


class JobResponse(BaseModel):
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
    error_message: str = Field(..., min_length=1)
    error_class: str = "transient"
    retry: bool | None = None


class TrainJobCreate(BaseModel):
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


class ExperimentBody(BaseModel):
    run_id: str | None = None
    dataset_version: str | None = None
    model_version: str | None = None
    git_commit: str | None = None
    hyperparams: dict[str, Any] | None = None
    output_path: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/models")
async def list_models(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Models on the LLM backend (Ollama tags, or vLLM served models).

    With Ollama, homelab-* models (publish-lora-to-ollama) are preferred as the default.
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
    try:
        return {"gpu": "ok", "backend": llm.active_backend(), "mode": llm.MODE, "models": await llm.list_models()}
    except Exception as e:
        return {"gpu": "down", "backend": llm.active_backend(), "mode": llm.MODE, "error": str(e)}


@app.get("/system/status")
def system_status(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
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
                mark_job_failed(UUID(job_id), str(e), error_class="transient")
            raise HTTPException(status_code=502, detail=str(e)) from e
        duration_ms = int((time.perf_counter() - started) * 1000)
        log_chat(body.prompt, text, model_used, duration_ms)
        if job_id:
            mark_job_succeeded(UUID(job_id))
        if _redis:
            _redis.incr("homelab:requests:total")
        return ChatResponse(model=model_used, response=text, duration_ms=duration_ms, job_id=job_id)
    except HTTPException:
        raise
    except Exception as e:
        if job_id:
            mark_job_failed(UUID(job_id), str(e), error_class="transient")
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.get("/stats")
def stats(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    require_api_key(x_api_key)
    total = int(_redis.get("homelab:requests:total") or 0) if _redis else 0
    return {"requests_total": total}


@app.post("/jobs", response_model=JobResponse)
def jobs_create_train(
    body: TrainJobCreate | None = None,
    x_api_key: str | None = Header(default=None),
) -> JobResponse:
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
    require_worker_key(x_worker_key)
    worker_id = x_worker_id or "unknown-worker"
    record_worker_heartbeat(worker_id, x_worker_host)
    return {"ok": "true", "worker_id": worker_id}


@app.get("/internal/worker/train/{job_id}/plan")
def worker_get_train_plan(
    job_id: UUID,
    x_worker_key: str | None = Header(default=None),
) -> dict[str, Any]:
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
    x_worker_key: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None, alias="X-Worker-Id"),
):
    require_worker_key(x_worker_key)
    worker_id = x_worker_id or "unknown-worker"
    record_worker_heartbeat(worker_id)
    try:
        job = claim_train_job(worker_id)
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
def worker_reset_stale(x_worker_key: str | None = Header(default=None)) -> dict[str, int]:
    require_worker_key(x_worker_key)
    try:
        expired = expire_stale_leases()
        forced = reset_stale_running_to_pending()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"lease_expired_requeued": expired, "forced_reset": forced}
