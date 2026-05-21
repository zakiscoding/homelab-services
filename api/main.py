import os
import time
from contextlib import asynccontextmanager
from typing import Any

import httpx
import psycopg
import redis
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

GPU_URL = os.environ.get("GPU_URL", "http://100.88.71.33:11434")
DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
API_KEY = os.environ.get("API_KEY", "")

_redis: redis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _redis
    if REDIS_URL:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    yield
    if _redis:
        _redis.close()


app = FastAPI(title="Homelab API", version="0.1.0", lifespan=lifespan)


def require_api_key(x_api_key: str | None) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


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
