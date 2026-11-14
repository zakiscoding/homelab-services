"""Tracing: spans are created, joined to an incoming trace, propagated to the LLM, and carry no prompt.

Runs without Postgres, Redis or a GPU: a two-route FastAPI app stands in for main.py and a
local HTTP server stands in for vLLM, recording the headers it receives. It is a real socket on
purpose: the httpx instrumentation hooks httpx's real transports, which a MockTransport bypasses.

    docker run --rm -v "$PWD/api:/api" -w /api python:3.12-slim sh -c \
      "pip install -q -r requirements.txt pytest && python -m pytest -q tests"
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import tracing

PROMPT = "a secret prompt that must not reach the trace backend"
INCOMING_TRACE = "4bf92f3577b34da6a3ce929d0e0e4736"
INCOMING = f"00-{INCOMING_TRACE}-00f067aa0ba902b7-01"


@pytest.fixture(scope="module")
def traced():
    """App + exporter + list of upstream request headers, instrumented once for the module
    (the global tracer provider can only be set once per process)."""
    seen: list[dict] = []

    class FakeVLLM(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers["content-length"]))
            seen.append({k.lower(): v for k, v in self.headers.items()})
            body = json.dumps({"choices": [{"message": {"content": "hi"}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), FakeVLLM)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    vllm_url = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"

    app = FastAPI()

    @app.post("/chat")
    async def chat(body: dict):
        async with httpx.AsyncClient() as client:
            r = await client.post(vllm_url,
                                  json={"messages": [{"role": "user", "content": body["prompt"]}]})
        tracing.annotate(**{"llm.backend": "vllm", "llm.model": None})
        return r.json()

    @app.get("/health")
    def health():
        return {"ok": True}

    exporter = InMemorySpanExporter()
    assert tracing.setup(app, exporter=exporter, processor_cls=SimpleSpanProcessor)
    yield TestClient(app), exporter, seen
    FastAPIInstrumentor.uninstrument_app(app)
    HTTPXClientInstrumentor().uninstrument()
    server.shutdown()


@pytest.fixture(autouse=True)
def _clear(traced):
    traced[1].clear()
    traced[2].clear()


def by_kind(spans, kind):
    return [s for s in spans if s.kind == kind]


def test_server_and_client_spans_share_one_trace(traced):
    client, exporter, _ = traced
    assert client.post("/chat", json={"prompt": PROMPT}).status_code == 200
    spans = exporter.get_finished_spans()
    server = by_kind(spans, trace.SpanKind.SERVER)
    upstream = by_kind(spans, trace.SpanKind.CLIENT)
    assert len(server) == 1 and len(upstream) == 1
    assert upstream[0].context.trace_id == server[0].context.trace_id
    assert upstream[0].parent.span_id == server[0].context.span_id
    assert server[0].attributes["llm.backend"] == "vllm"
    assert "llm.model" not in server[0].attributes  # None values are skipped
    assert server[0].resource.attributes["service.name"] == "homelab-api"


def test_traceparent_reaches_the_llm(traced):
    client, exporter, seen = traced
    client.post("/chat", json={"prompt": PROMPT})
    upstream = by_kind(exporter.get_finished_spans(), trace.SpanKind.CLIENT)[0]
    header = seen[0]["traceparent"]
    version, trace_id, span_id, _flags = header.split("-")
    assert version == "00"
    assert int(trace_id, 16) == upstream.context.trace_id
    assert int(span_id, 16) == upstream.context.span_id


def test_incoming_trace_is_continued(traced):
    client, exporter, seen = traced
    client.post("/chat", json={"prompt": PROMPT}, headers={"traceparent": INCOMING})
    for span in exporter.get_finished_spans():
        assert format(span.context.trace_id, "032x") == INCOMING_TRACE
    assert seen[0]["traceparent"].split("-")[1] == INCOMING_TRACE


def test_no_prompt_or_key_in_any_span(traced):
    client, exporter, _ = traced
    client.post("/chat", json={"prompt": PROMPT}, headers={"x-api-key": "k-should-not-appear"})
    for span in exporter.get_finished_spans():
        dump = repr(dict(span.attributes)) + repr([dict(e.attributes) for e in span.events])
        assert PROMPT not in dump
        assert "k-should-not-appear" not in dump


def test_health_is_not_traced(traced):
    client, exporter, _ = traced
    client.get("/health")
    assert exporter.get_finished_spans() == ()


def test_off_without_endpoint(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    assert tracing.setup(FastAPI()) is False
