"""LLM backend selection under a lossy network (the iMac's Colima VM drops some connections).

httpx.get is replaced with a scripted fake, so each test states exactly which connection attempts
fail. The Ollama timeout test uses a real local server that never answers in time.
"""

import asyncio
import socket
import threading
import time

import httpx
import pytest

import llm

PRIMARY = ("http://ubuntu.test:8000/v1", "k1")
FALLBACK = ("http://wsl.test:8000/v1", "k2")


class Script:
    """Fake httpx.get: per URL, a list of outcomes consumed in order ("ok", 401, "drop")."""

    def __init__(self, **plans):
        self.plans = {k: list(v) for k, v in plans.items()}
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        host = url.split("//")[1].split(".")[0]
        self.calls.append(host)
        outcome = self.plans[host].pop(0) if self.plans[host] else "drop"
        if outcome == "drop":
            raise httpx.ConnectTimeout("dropped")
        return httpx.Response(200 if outcome == "ok" else outcome)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_ENDPOINTS", [PRIMARY, FALLBACK])
    monkeypatch.setattr(llm, "PROBE_ATTEMPTS", 3)
    monkeypatch.setattr(llm, "_ensure_prober", lambda: None)  # no background thread in tests
    monkeypatch.setattr(llm, "_auto", {"checked_at": 0.0, "endpoint": None, "misses": 0})
    monkeypatch.setattr(llm, "MODE", "auto")


def test_dropped_connections_are_retried(monkeypatch):
    fake = Script(wsl=["drop", "drop", "ok"])
    monkeypatch.setattr(llm.httpx, "get", fake)
    assert llm._answers(*FALLBACK) is True
    assert fake.calls == ["wsl"] * 3


def test_http_answers_are_final(monkeypatch):
    fake = Script(wsl=[401, "ok"])
    monkeypatch.setattr(llm.httpx, "get", fake)
    assert llm._answers(*FALLBACK) is False
    assert fake.calls == ["wsl"]  # a wrong key isn't retried


def test_gives_up_after_the_attempts(monkeypatch):
    fake = Script(wsl=["drop"] * 10)
    monkeypatch.setattr(llm.httpx, "get", fake)
    assert llm._answers(*FALLBACK) is False
    assert len(fake.calls) == 3


def test_offline_primary_falls_through_to_the_fallback(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", Script(ubuntu=[], wsl=["drop", "ok"]))
    assert llm.refresh() == FALLBACK
    assert llm.active_backend() == "openai"


def test_one_bad_check_does_not_switch_to_ollama(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", Script(wsl=["ok"]))
    assert llm.refresh() == FALLBACK
    monkeypatch.setattr(llm.httpx, "get", Script())  # everything drops
    assert llm.refresh() == FALLBACK  # kept for one round
    assert llm.refresh() is None      # second miss in a row: really down
    assert llm.active_backend() == "ollama"


def test_recovery_resets_the_miss_count(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", Script(wsl=["ok"]))
    llm.refresh()
    monkeypatch.setattr(llm.httpx, "get", Script())
    llm.refresh()
    monkeypatch.setattr(llm.httpx, "get", Script(wsl=["ok"]))
    llm.refresh()
    monkeypatch.setattr(llm.httpx, "get", Script())
    assert llm.refresh() == FALLBACK  # one miss again, still kept


def test_requests_do_not_probe_after_the_first_check(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", Script(wsl=["ok"]))
    assert llm.active_backend() == "openai"  # first call checks synchronously
    fake = Script()
    monkeypatch.setattr(llm.httpx, "get", fake)
    for _ in range(5):
        assert llm.active_backend() == "openai"
    assert fake.calls == []  # served from the background-maintained cache


def test_dead_ollama_fails_fast_with_llm_error(monkeypatch):
    # A server that accepts the connection and never replies, like the hung Ollama.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    held = []
    threading.Thread(target=lambda: held.append(server.accept()), daemon=True).start()
    monkeypatch.setattr(llm, "GPU_URL", f"http://127.0.0.1:{server.getsockname()[1]}")
    monkeypatch.setattr(llm, "OLLAMA_TIMEOUT", httpx.Timeout(1.0, connect=1.0))
    monkeypatch.setattr(llm.httpx, "get", Script())  # no vLLM answers -> Ollama
    started = time.monotonic()
    with pytest.raises(llm.LLMError, match="did not answer"):
        asyncio.run(llm.generate("llama3.2:latest", "hi"))
    assert time.monotonic() - started < 5
    server.close()
