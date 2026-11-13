"""LLM backend for /chat, /rag and status: Ollama (default) or an OpenAI-compatible server.

LLM_BACKEND=ollama  -> Ollama on GPU_URL (Windows boot of the GPU PC): /api/generate, /api/tags
LLM_BACKEND=openai  -> vLLM (or any OpenAI-compatible server) on LLM_URL, e.g. the GPU PC's
                       Ubuntu boot: http://100.121.51.35:8000/v1 with LLM_API_KEY
LLM_BACKEND=auto    -> vLLM when it answers, else Ollama. The GPU PC dual-boots, so chat follows
                       whichever OS is up. Re-checked at most every AUTO_RECHECK_S seconds.

Optional second vLLM: LLM_FALLBACK_URL / LLM_FALLBACK_API_KEY. In openai/auto mode the first of
LLM_URL, LLM_FALLBACK_URL that answers is used. On the GPU PC's Windows boot, vLLM runs in WSL
and zakar's Tailscale serves it at http://100.88.71.33:8000/v1 with its own key.

With the openai backend, a requested model the server doesn't serve (e.g. the UI's
"llama3.2:latest" default) falls back to LLM_DEFAULT_MODEL, and callers get the model name
that actually answered.
"""

import os
import time
from typing import Any

import httpx

MODE = os.environ.get("LLM_BACKEND", "ollama").strip().lower()
AUTO_RECHECK_S = 30.0
GPU_URL = os.environ.get("GPU_URL", "http://100.88.71.33:11434")
LLM_URL = os.environ.get("LLM_URL", "http://100.121.51.35:8000/v1").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_FALLBACK_URL = os.environ.get("LLM_FALLBACK_URL", "").rstrip("/")
LLM_FALLBACK_API_KEY = os.environ.get("LLM_FALLBACK_API_KEY", "")
# (base_url, api_key) in preference order
OPENAI_ENDPOINTS = [(LLM_URL, LLM_API_KEY)] + ([(LLM_FALLBACK_URL, LLM_FALLBACK_API_KEY)] if LLM_FALLBACK_URL else [])
LLM_DEFAULT_MODEL = os.environ.get("LLM_DEFAULT_MODEL", "qwen2.5-1.5b-instruct")
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "1024"))

OLLAMA_DEFAULT_MODEL = "llama3.2:latest"


class LLMError(Exception):
    """Upstream LLM failure; message is safe to return as an HTTP 502 detail."""


_auto: dict[str, Any] = {"checked_at": 0.0, "endpoint": None}


def _answers(url: str, key: str) -> bool:
    """True if an OpenAI-compatible server answers GET /models with 200 within 2s."""
    try:
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        return httpx.get(f"{url}/models", headers=headers, timeout=2.0).status_code == 200
    except Exception:
        return False


def _openai_endpoint() -> tuple[str, str] | None:
    """First OPENAI_ENDPOINTS entry that answers, cached for AUTO_RECHECK_S seconds."""
    if time.monotonic() - _auto["checked_at"] > AUTO_RECHECK_S:
        _auto["endpoint"] = next((e for e in OPENAI_ENDPOINTS if _answers(*e)), None)
        _auto["checked_at"] = time.monotonic()
    return _auto["endpoint"]


def _openai() -> tuple[str, str]:
    """(url, key) to use for OpenAI-compatible calls; the primary when none answers."""
    return _openai_endpoint() or OPENAI_ENDPOINTS[0]


def is_openai() -> bool:
    """Whether requests should go to the OpenAI-compatible backend.

    Fixed by LLM_BACKEND for ``openai``/``ollama``; in ``auto`` mode the probe result is cached
    for AUTO_RECHECK_S seconds.
    """
    if MODE != "auto":
        return MODE == "openai"
    return _openai_endpoint() is not None


def active_backend() -> str:
    """'openai' or 'ollama' — what the next request will use."""
    return "openai" if is_openai() else "ollama"


def backend_url() -> str:
    """Base URL of the active backend (the answering vLLM URL, or GPU_URL)."""
    return _openai()[0] if is_openai() else GPU_URL


def _headers() -> dict[str, str]:
    """Bearer auth header for the active OpenAI-compatible server, if it has a key."""
    key = _openai()[1]
    return {"Authorization": f"Bearer {key}"} if key else {}


async def list_models() -> list[str]:
    """Model names served by the active backend (vLLM ``/models`` or Ollama ``/api/tags``).

    Raises httpx errors on connection failure or a non-2xx response.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        if is_openai():
            r = await client.get(f"{_openai()[0]}/models", headers=_headers())
            r.raise_for_status()
            return [m["id"] for m in r.json().get("data", []) if m.get("id")]
        r = await client.get(f"{GPU_URL}/api/tags")
        r.raise_for_status()
        return [m.get("name", "") for m in r.json().get("models", []) if m.get("name")]


def default_model() -> str:
    """Default chat model for the active backend (LLM_DEFAULT_MODEL or llama3.2:latest)."""
    return LLM_DEFAULT_MODEL if is_openai() else OLLAMA_DEFAULT_MODEL


async def generate(model: str, prompt: str) -> tuple[str, str]:
    """Single-turn completion. Returns (text, model_used).

    Raises LLMError on an upstream 4xx/5xx; connection errors propagate as httpx exceptions.
    """
    async with httpx.AsyncClient(timeout=300.0) as client:
        if not is_openai():
            r = await client.post(
                f"{GPU_URL}/api/generate", json={"model": model, "prompt": prompt, "stream": False}
            )
            if r.status_code >= 400:
                raise LLMError(r.text)
            return r.json().get("response", ""), model

        served = await list_models()
        used = model if model in served else (LLM_DEFAULT_MODEL if LLM_DEFAULT_MODEL in served else served[0])
        r = await client.post(
            f"{_openai()[0]}/chat/completions",
            headers=_headers(),
            json={
                "model": used,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": LLM_MAX_TOKENS,
            },
        )
        if r.status_code >= 400:
            raise LLMError(r.text)
        data: dict[str, Any] = r.json()
        return data["choices"][0]["message"]["content"] or "", used


def ping(timeout: float = 5.0) -> bool:
    """Synchronous liveness check for /system/status."""
    try:
        if is_openai():
            r = httpx.get(f"{_openai()[0]}/models", headers=_headers(), timeout=timeout)
        else:
            r = httpx.get(f"{GPU_URL}/api/tags", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False
