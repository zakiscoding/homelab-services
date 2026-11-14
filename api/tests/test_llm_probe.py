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
        outcome = self.plans[host].pop(0) if self.plans.get(host) else "drop"
        if outcome == "drop":
            raise httpx.ConnectTimeout("dropped")
        return httpx.Response(200 if outcome == "ok" else outcome)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(llm, "OPENAI_ENDPOINTS", [PRIMARY, FALLBACK])
    monkeypatch.setattr(llm, "PROBE_ATTEMPTS", 3)
    monkeypatch.setattr(llm, "_ensure_prober", lambda: None)  # no background thread in tests
    monkeypatch.setattr(llm, "_auto", {"checked_at": 0.0, "endpoint": None, "misses": 0, "last_good": None})
    monkeypatch.setattr(llm, "GPU_URL", "http://ollama.test:11434")
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
    monkeypatch.setattr(llm.httpx, "get", Script(ollama=["ok"] * 5))  # vLLM drops, Ollama fine
    assert llm.refresh() == FALLBACK  # kept for one round
    assert llm.refresh() is None      # second miss in a row: really down, and Ollama answers
    assert llm.active_backend() == "ollama"


def test_stays_on_vllm_when_ollama_is_dead_too(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", Script(wsl=["ok"]))
    assert llm.refresh() == FALLBACK
    monkeypatch.setattr(llm.httpx, "get", Script())  # everything drops, Ollama included
    llm.refresh()
    assert llm.refresh() == FALLBACK  # no point switching to a fallback that can't answer
    assert llm.active_backend() == "openai"


def test_never_seen_vllm_and_dead_ollama_means_ollama(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", Script())
    assert llm.refresh() is None  # nothing has ever worked: no vLLM to stay on


def test_recovery_resets_the_miss_count(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", Script(wsl=["ok"]))
    llm.refresh()
    monkeypatch.setattr(llm.httpx, "get", Script(ollama=["ok"] * 5))
    llm.refresh()
    monkeypatch.setattr(llm.httpx, "get", Script(wsl=["ok"]))
    llm.refresh()
    monkeypatch.setattr(llm.httpx, "get", Script(ollama=["ok"] * 5))
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


def test_chat_reuses_the_model_list(monkeypatch):
    """One connection per chat instead of two: /models is fetched once per TTL."""
    calls = []

    async def fake_list_models():
        calls.append(1)
        return ["qwen2.5-1.5b-instruct"]

    monkeypatch.setattr(llm, "_served", {})
    monkeypatch.setattr(llm, "list_models", fake_list_models)
    for _ in range(3):
        assert asyncio.run(llm._served_models("http://wsl.test:8000/v1")) == ["qwen2.5-1.5b-instruct"]
    assert len(calls) == 1


def test_endpoint_that_answered_last_is_probed_first(monkeypatch):
    # The PC is in the Windows boot: the Ubuntu-boot primary is an offline peer. After one full
    # check, later checks must not dial it while the fallback keeps answering (each dead dial
    # holds one of Colima's 10 pending-connect slots for over a minute).
    monkeypatch.setattr(llm.httpx, "get", Script(ubuntu=[], wsl=["ok"]))
    assert llm.refresh() == FALLBACK
    fake = Script(wsl=["ok"] * 3)
    monkeypatch.setattr(llm.httpx, "get", fake)
    for _ in range(3):
        assert llm.refresh() == FALLBACK
    assert fake.calls == ["wsl"] * 3


def test_primary_is_probed_again_when_the_last_good_one_stops(monkeypatch):
    monkeypatch.setattr(llm.httpx, "get", Script(wsl=["ok"]))
    assert llm.refresh() == FALLBACK
    fake = Script(wsl=[], ubuntu=["ok"])  # rebooted into Ubuntu
    monkeypatch.setattr(llm.httpx, "get", fake)
    assert llm.refresh() == PRIMARY
    assert fake.calls == ["wsl"] * 3 + ["ubuntu"]
