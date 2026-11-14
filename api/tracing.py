"""OpenTelemetry tracing for the API: one trace per request, continued into vLLM.

Off unless OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g. http://otel-collector:4318). When on:

* FastAPI server spans for every route except /health and /metrics.
* httpx client spans for every upstream call (vLLM, Ollama, the GPU vision service), with a W3C
  ``traceparent`` header injected, so vLLM's own spans join the same trace.
* An incoming ``traceparent`` (from the Cloudflare gateway or the web UI) is continued, not replaced.

Spans carry routes, status codes and timings only. Request and response bodies (prompts,
completions) and headers (keys) are never recorded: the FastAPI and httpx instrumentations don't
capture bodies, and header capture is left at its default (off).
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter

EXCLUDED_URLS = "health,metrics"


def setup(app, exporter: SpanExporter | None = None, processor_cls=BatchSpanProcessor) -> bool:  # noqa: ANN001
    """Instrument `app` and outgoing httpx calls. Returns False (and does nothing) when tracing is off.

    `exporter` and `processor_cls` exist for tests (in-memory exporter, synchronous processor).
    """
    if exporter is None and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return False

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    if exporter is None:
        # Reads OTEL_EXPORTER_OTLP_ENDPOINT and appends /v1/traces.
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter()

    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME", "homelab-api"),
        "deployment.environment": os.environ.get("OTEL_DEPLOYMENT_ENVIRONMENT", "homelab"),
    })
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(processor_cls(exporter))
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls=EXCLUDED_URLS)
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    return True


def annotate(**attributes) -> None:  # noqa: ANN003
    """Add attributes (e.g. the LLM backend that answered) to the current span, if any."""
    span = trace.get_current_span()
    if span.is_recording():
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
