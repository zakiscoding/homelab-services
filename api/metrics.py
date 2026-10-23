"""Phase 5 — Prometheus metrics (scrape /metrics; Postgres-backed job gauges)."""

from __future__ import annotations

import os
import time
from typing import Callable

import psycopg
from prometheus_client import Counter, Gauge, Histogram, generate_latest

DATABASE_URL = os.environ.get("DATABASE_URL", "")
REDIS_URL = os.environ.get("REDIS_URL", "")

HTTP_REQUESTS = Counter(
    "homelab_http_requests_total",
    "HTTP requests",
    ["method", "endpoint", "status"],
)
HTTP_LATENCY = Histogram(
    "homelab_http_request_duration_seconds",
    "HTTP request latency",
    ["method", "endpoint"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 60.0, 120.0, 300.0),
)
JOBS_PENDING = Gauge("homelab_jobs_pending", "Pending jobs", ["type"])
JOBS_RUNNING = Gauge("homelab_jobs_running", "Running jobs", ["type"])
JOBS_FAILED_24H = Gauge("homelab_jobs_failed_24h", "Failed jobs in last 24h", ["type"])
WORKER_LAST_SEEN_AGE = Gauge(
    "homelab_worker_last_seen_age_seconds",
    "Seconds since worker heartbeat",
    ["worker_id"],
)
REDIS_REQUESTS = Gauge("homelab_redis_requests_total", "Total /chat requests via Redis counter")
JOB_NOTIFY_ENABLED = Gauge(
    "homelab_job_notify_enabled",
    "1 if Redis job notify is enabled",
)
JOB_NOTIFY_QUEUE = Gauge(
    "homelab_job_notify_queue_depth",
    "Redis wake queue length",
    ["queue"],
)
VISION_REQUESTS = Counter(
    "homelab_vision_requests_total",
    "Reaction vision proxy requests",
    ["endpoint", "status"],
)
VISION_LATENCY = Histogram(
    "homelab_vision_request_duration_seconds",
    "Reaction vision proxy latency",
    ["endpoint"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 15.0, 30.0),
)
VISION_GPU_UP = Gauge(
    "homelab_vision_gpu_reachable",
    "1 if GPU reaction service health check passed",
)

_last_refresh = 0.0
_REFRESH_SEC = float(os.environ.get("METRICS_REFRESH_SEC", "15"))


def _connect():
    """Open a psycopg connection, or None when DATABASE_URL is unset (metrics then skip DB gauges)."""
    if not DATABASE_URL:
        return None
    return psycopg.connect(DATABASE_URL)


def refresh_gauges() -> None:
    """Refresh job, worker, Redis and wake-queue gauges, at most once per METRICS_REFRESH_SEC.

    Read-only queries against ``jobs`` and ``worker_heartbeats``; Redis errors are ignored. Label
    sets are only updated when a row exists, so a job type with no pending/running (or recent
    failed) rows keeps its last gauge value.
    """
    global _last_refresh
    now = time.time()
    if now - _last_refresh < _REFRESH_SEC:
        return
    _last_refresh = now

    try:
        from job_notify import WAKE_IMAC, WAKE_TRAIN, status as notify_status

        ns = notify_status()
        JOB_NOTIFY_ENABLED.set(1 if ns.get("enabled") else 0)
        for queue, depth in (ns.get("queues") or {}).items():
            JOB_NOTIFY_QUEUE.labels(queue=queue).set(depth)
    except Exception:
        JOB_NOTIFY_ENABLED.set(0)

    if REDIS_URL:
        try:
            import redis

            rds = redis.from_url(REDIS_URL, decode_responses=True)
            REDIS_REQUESTS.set(int(rds.get("homelab:requests:total") or 0))
        except Exception:
            pass

    conn = _connect()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT type, status, COUNT(*) FROM jobs
                GROUP BY type, status
                """
            )
            pending: dict[str, float] = {}
            running: dict[str, float] = {}
            for job_type, status, count in cur.fetchall():
                if status == "pending":
                    pending[job_type] = count
                elif status == "running":
                    running[job_type] = count
            for t in set(pending) | set(running):
                JOBS_PENDING.labels(type=t).set(pending.get(t, 0))
                JOBS_RUNNING.labels(type=t).set(running.get(t, 0))

            cur.execute(
                """
                SELECT type, COUNT(*) FROM jobs
                WHERE status = 'failed' AND finished_at > NOW() - INTERVAL '24 hours'
                GROUP BY type
                """
            )
            for job_type, count in cur.fetchall():
                JOBS_FAILED_24H.labels(type=job_type).set(count)

            cur.execute(
                """
                SELECT worker_id,
                       EXTRACT(EPOCH FROM (NOW() - last_seen_at))::float AS age
                FROM worker_heartbeats
                """
            )
            for worker_id, age in cur.fetchall():
                WORKER_LAST_SEEN_AGE.labels(worker_id=worker_id).set(age or 0)
    finally:
        conn.close()


def metrics_body() -> bytes:
    """Refresh gauges and return the Prometheus text exposition for GET /metrics."""
    refresh_gauges()
    return generate_latest()


def prometheus_middleware(app):  # noqa: ANN001
    """Record request count/latency; skip /metrics and /health.

    Also skips the OpenAPI/docs paths. Endpoint labels are the raw path truncated to 64 chars, so
    paths with ids (e.g. /jobs/{id}) produce one series per id.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import Response

    class _Mw(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Callable) -> Response:
            path = request.url.path
            if path in ("/metrics", "/health", "/openapi.json", "/docs", "/redoc"):
                return await call_next(request)
            method = request.method
            start = time.perf_counter()
            response = await call_next(request)
            elapsed = time.perf_counter() - start
            endpoint = path.split("?")[0]
            if len(endpoint) > 64:
                endpoint = endpoint[:64]
            HTTP_REQUESTS.labels(
                method=method, endpoint=endpoint, status=str(response.status_code)
            ).inc()
            HTTP_LATENCY.labels(method=method, endpoint=endpoint).observe(elapsed)
            return response

    app.add_middleware(_Mw)
    return app
