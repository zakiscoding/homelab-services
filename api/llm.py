"""LLM backend for /chat, /rag and status: Ollama (default) or an OpenAI-compatible server.

LLM_BACKEND=ollama  -> Ollama on GPU_URL (Windows boot of the GPU PC): /api/generate, /api/tags
LLM_BACKEND=openai  -> vLLM (or any OpenAI-compatible server) on LLM_URL, e.g. the GPU PC's
                       Ubuntu boot: http://100.121.51.35:8000/v1 with LLM_API_KEY
LLM_BACKEND=auto    -> vLLM when it answers, else Ollama. The GPU PC dual-boots, so chat follows
                       whichever OS is up. A background thread re-checks every AUTO_RECHECK_S
                       seconds, so requests never wait on the check.

Each check retries a dropped connection (PROBE_ATTEMPTS x PROBE_TIMEOUT_S), and a working vLLM
endpoint is kept until it fails two checks in a row. Both exist because the iMac's Colima VM drops
or delays some outbound connections (docs/OBSERVABILITY-SLO.md): with one 2 s attempt, most checks
failed and chat fell back to Ollama even while vLLM was healthy.

Optional second vLLM: LLM_FALLBACK_URL / LLM_FALLBACK_API_KEY. In openai/auto mode the first of
LLM_URL, LLM_FALLBACK_URL that answers is used. On the GPU PC's Windows boot, vLLM runs in WSL
and zakar's Tailscale serves it at http://100.88.71.33:8000/v1 with its own key.

With the openai backend, a requested model the server doesn't serve (e.g. the UI's
"llama3.2:latest" default) falls back to LLM_DEFAULT_MODEL, and callers get the model name
that actually answered.
"""

import os
import threading
import time
from typing import Any

import httpx

import tracing

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
PROBE_ATTEMPTS = int(os.environ.get("LLM_PROBE_ATTEMPTS", "3"))
PROBE_TIMEOUT_S = float(os.environ.get("LLM_PROBE_TIMEOUT_S", "3"))
# A dead Ollama used to hold a chat request for 133 s before failing; fail in a bounded time instead.
OLLAMA_TIMEOUT = httpx.Timeout(float(os.environ.get("OLLAMA_TIMEOUT_S", "60")), connect=5.0)

OLLAMA_DEFAULT_MODEL = "llama3.2:latest"


class LLMError(Exception):
    """Upstream LLM failure; message is safe to return as an HTTP 502 detail."""


_auto: dict[str, Any] = {"checked_at": 0.0, "endpoint": None, "misses": 0, "last_good": None}
_lock = threading.Lock()
_prober: threading.Thread | None = None


def _answers(url: str, key: str) -> bool:
    """True if an OpenAI-compatible server answers GET /models with 200.

    Retries only when the connection itself fails (timeout, reset, refused). An HTTP answer is
    final: a 401 means a wrong key, and retrying won't change that.
    """
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    for _ in range(PROBE_ATTEMPTS):
        try:
            return httpx.get(f"{url}/models", headers=headers, timeout=PROBE_TIMEOUT_S).status_code == 200
        except httpx.TransportError:
            continue
        except Exception:
            return False
    return False


def _ollama_answers() -> bool:
    """True if Ollama answers GET /api/tags (same retry rule as _answers)."""
    for _ in range(PROBE_ATTEMPTS):
        try:
            return httpx.get(f"{GPU_URL}/api/tags", timeout=PROBE_TIMEOUT_S).status_code == 200
        except httpx.TransportError:
            continue
        except Exception:
            return False
    return False


def refresh() -> tuple[str, str] | None:
    """Re-check the endpoints and update the cached choice. Returns the endpoint now in use.

    - Hysteresis: if the current endpoint stops answering but did on the previous check, keep it
      for one more round, so a single bad check doesn't switch chat to Ollama.
    - Only fall back to Ollama if Ollama itself answers. When neither answers (as when the
      network drops connections for a minute or two), stay on the last vLLM that worked: chat
      requests there have long timeouts and usually get through a slow connect, while a dead
      Ollama can only fail.
    """
    found = next((e for e in OPENAI_ENDPOINTS if _answers(*e)), None)
    with _lock:
        current = _auto["endpoint"]
        if found is not None:
            _auto["endpoint"], _auto["last_good"], _auto["misses"] = found, found, 0
        elif current is not None and _auto["misses"] < 1:
            _auto["misses"] += 1
        else:
            _auto["misses"] = 0
            _auto["endpoint"] = None
        last_good = _auto["last_good"]
    if found is None and _auto["endpoint"] is None and last_good is not None and MODE == "auto":
        if not _ollama_answers():
            with _lock:
                _auto["endpoint"] = last_good
    with _lock:
        _auto["checked_at"] = time.monotonic()
        return _auto["endpoint"]


def _probe_forever() -> None:
    while True:
        time.sleep(AUTO_RECHECK_S)
        try:
            refresh()
        except Exception:  # never let the prober die
            pass


def _ensure_prober() -> None:
    global _prober
    with _lock:
        if _prober is None:
            _prober = threading.Thread(target=_probe_forever, name="llm-prober", daemon=True)
            _prober.start()


def _openai_endpoint() -> tuple[str, str] | None:
    """The OPENAI_ENDPOINTS entry chat should use, or None. Checked synchronously only the first
    time; after that the background prober keeps it current."""
    if _auto["checked_at"] == 0.0:
        refresh()
        _ensure_prober()
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


_served: dict[str, tuple[float, list[str]]] = {}
SERVED_TTL_S = 300.0


async def _served_models(url: str) -> list[str]:
    """vLLM's model list, cached per endpoint for SERVED_TTL_S. Saves one connection per chat,
    which matters on a network that drops some of them; a stale list only affects which name
    the request asks for, and vLLM rejects an unknown one with a clear error."""
    cached = _served.get(url)
    if cached and time.monotonic() - cached[0] < SERVED_TTL_S:
        return cached[1]
    models = await list_models()
    _served[url] = (time.monotonic(), models)
    return models


def default_model() -> str:
    """Default chat model for the active backend (LLM_DEFAULT_MODEL or llama3.2:latest)."""
    return LLM_DEFAULT_MODEL if is_openai() else OLLAMA_DEFAULT_MODEL


async def generate(model: str, prompt: str) -> tuple[str, str]:
    """Single-turn completion. Returns (text, model_used).

    Raises LLMError on an upstream 4xx/5xx; connection errors propagate as httpx exceptions.
    """
    async with httpx.AsyncClient(timeout=300.0) as client:
        if not is_openai():
            try:
                r = await client.post(
                    f"{GPU_URL}/api/generate", json={"model": model, "prompt": prompt, "stream": False},
                    timeout=OLLAMA_TIMEOUT,
                )
            except httpx.ConnectTimeout as e:
                tracing.annotate(**{"llm.backend": "ollama", "llm.model": model})
                raise LLMError(f"Ollama unreachable: no connection within {OLLAMA_TIMEOUT.connect:.0f} s") from e
            except httpx.TimeoutException as e:
                tracing.annotate(**{"llm.backend": "ollama", "llm.model": model})
                raise LLMError(f"Ollama did not answer within {OLLAMA_TIMEOUT.read:.0f} s") from e
            tracing.annotate(**{"llm.backend": "ollama", "llm.model": model})
            if r.status_code >= 400:
                raise LLMError(r.text)
            return r.json().get("response", ""), model

        served = await _served_models(_openai()[0])
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
        tracing.annotate(**{"llm.backend": "vllm", "llm.endpoint": _openai()[0], "llm.model": used})
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
