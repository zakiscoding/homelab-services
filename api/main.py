import os
import time
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
import psycopg
import redis
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from jobs import (
    claim_train_job,
    create_train_job,
    get_job,
    mark_job_failed,
    mark_job_succeeded,
    reset_stale_running_to_pending,
)

GPU_URL = os.environ.get("GPU_URL", "http://100.88.71.33:11434")
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


app = FastAPI(title="Homelab API", version="0.2.0", lifespan=lifespan)


def require_api_key(x_api_key: str | None) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def require_worker_key(x_worker_key: str | None) -> None:
    if not WORKER_API_KEY:
        raise HTTPException(status_code=503, detail="WORKER_API_KEY not configured")
    if x_worker_key != WORKER_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing worker API key")


def log_chat(prompt: str, response: str, model: str, duration_ms: int) -> None:
    if not DATABASE_URL:
        return
    try:
        with psycopg.connect(DATABASE_URL) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_logs (prompt, response, model, duration_ms)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (prompt, response, model, duration_ms),
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/gpu")
async def health_gpu() -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{GPU_URL}/api/tags")
        r.raise_for_status()
        return {"gpu": "ok", "models": r.json()}


@app.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    x_api_key: str | None = Header(default=None),
) -> ChatResponse:
    require_api_key(x_api_key)
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=300.0) as client:
        r = await client.post(
            f"{GPU_URL}/api/generate",
            json={
                "model": body.model,
                "prompt": body.prompt,
                "stream": False,
            },
        )
        if r.status_code >= 400:
            raise HTTPException(status_code=502, detail=r.text)
        data = r.json()
    duration_ms = int((time.perf_counter() - started) * 1000)
    text = data.get("response", "")
    log_chat(body.prompt, text, body.model, duration_ms)
    if _redis:
        _redis.incr("homelab:requests:total")
    return ChatResponse(model=body.model, response=text, duration_ms=duration_ms)


@app.get("/stats")
def stats(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    require_api_key(x_api_key)
    total = int(_redis.get("homelab:requests:total") or 0) if _redis else 0
    return {"requests_total": total}


# --- Phase 0A: job control plane ---


class JobResponse(BaseModel):
    id: str
    type: str
    status: str
    locked_by: str | None = None
    error_message: str | None = None
    created_at: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class JobFailBody(BaseModel):
    error_message: str = Field(..., min_length=1)


@app.post("/jobs", response_model=JobResponse)
def jobs_create_train(x_api_key: str | None = Header(default=None)) -> JobResponse:
    """Enqueue a GPU train job (Phase 0A — type train only)."""
    require_api_key(x_api_key)
    try:
        job = create_train_job()
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


@app.post("/internal/worker/jobs/claim", response_model=None)
def worker_claim_train(
    x_worker_key: str | None = Header(default=None),
    x_worker_id: str | None = Header(default=None, alias="X-Worker-Id"),
):
    """GPU worker claims one pending train job (SKIP LOCKED)."""
    require_worker_key(x_worker_key)
    worker_id = x_worker_id or "unknown-worker"
    try:
        job = claim_train_job(worker_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    if not job:
        return Response(status_code=204)
    return JobResponse(**job)


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
    job = mark_job_failed(job_id, body.error_message)
    if not job:
        raise HTTPException(status_code=409, detail="Job not found or not updatable")
    return JobResponse(**job)


@app.post("/internal/worker/jobs/reset-stale")
def worker_reset_stale(x_worker_key: str | None = Header(default=None)) -> dict[str, int]:
    """After kill -9: move running train jobs back to pending."""
    require_worker_key(x_worker_key)
    try:
        n = reset_stale_running_to_pending()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return {"reset_count": n}
