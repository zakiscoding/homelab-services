"""Reaction vision — proxy to GPU CNN inference service."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(prefix="/vision", tags=["vision"])

GPU_VISION_URL = os.environ.get("GPU_VISION_URL", "http://100.88.71.33:8100").rstrip("/")
REACTION_SERVE_KEY = os.environ.get("REACTION_SERVE_KEY", "")
API_KEY = os.environ.get("API_KEY", "")


def _require_api_key(x_api_key: str | None) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _vision_headers() -> dict[str, str]:
    h: dict[str, str] = {}
    if REACTION_SERVE_KEY:
        h["X-Reaction-Key"] = REACTION_SERVE_KEY
    return h


class ReactionRequest(BaseModel):
    image_b64: str = Field(..., min_length=32)
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class ReactionResponse(BaseModel):
    label: str | None = None
    confidence: float = 0.0
    reason: str
    face_detected: bool = False
    probabilities: dict[str, float] = Field(default_factory=dict)
    duration_ms: int = 0
    bbox: dict[str, int] | None = None


@router.get("/status")
async def vision_status(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """GPU reaction model health + metadata."""
    _require_api_key(x_api_key)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            health = await client.get(f"{GPU_VISION_URL}/health")
            health.raise_for_status()
            info = await client.get(f"{GPU_VISION_URL}/info", headers=_vision_headers())
            info.raise_for_status()
        return {"vision": "ok", "gpu_vision_url": GPU_VISION_URL, **info.json()}
    except Exception as e:
        return {"vision": "down", "gpu_vision_url": GPU_VISION_URL, "error": str(e)}


@router.post("/reaction", response_model=ReactionResponse)
async def vision_reaction(
    body: ReactionRequest,
    x_api_key: str | None = Header(default=None),
) -> ReactionResponse:
    """Classify facial expression from a webcam frame (base64 JPEG/PNG)."""
    _require_api_key(x_api_key)
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                f"{GPU_VISION_URL}/infer/reaction",
                json=body.model_dump(exclude_none=True),
                headers=_vision_headers(),
            )
            if r.status_code >= 400:
                raise HTTPException(status_code=502, detail=r.text)
            data = r.json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"GPU reaction service unreachable at {GPU_VISION_URL}. "
            f"On zak_pc run: homelab-gpu/scripts/start-reaction-serve-bg.ps1 ({e})",
        ) from e
    total_ms = int((time.perf_counter() - started) * 1000)
    if "duration_ms" not in data:
        data["duration_ms"] = total_ms
    return ReactionResponse(**data)
