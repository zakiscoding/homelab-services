"""
Phase 4 — Redis wake queues for workers (notify only).

Postgres remains the job state source of truth. Workers still claim via API.
"""

from __future__ import annotations

import os
from typing import Any

import redis

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
JOB_NOTIFY_ENABLED = os.environ.get("JOB_NOTIFY_ENABLED", "true").lower() == "true"

# LPUSH/BLPOP queues — not authoritative state
WAKE_TRAIN = "homelab:jobs:wake:train"
WAKE_IMAC = "homelab:jobs:wake:imac"
MAX_QUEUE_LEN = 1000

TRAIN_TYPES = frozenset({"train"})
IMAC_TYPES = frozenset({"ingest", "embed"})

_client: redis.Redis | None = None


def _redis() -> redis.Redis | None:
    global _client
    if not JOB_NOTIFY_ENABLED or not REDIS_URL:
        return None
    if _client is None:
        _client = redis.from_url(REDIS_URL, decode_responses=True)
    return _client


def queue_for_type(job_type: str) -> str | None:
    if job_type in TRAIN_TYPES:
        return WAKE_TRAIN
    if job_type in IMAC_TYPES:
        return WAKE_IMAC
    return None


def notify_job_pending(job_type: str, job_id: str) -> bool:
    """Signal workers that a claimable job may exist. Returns True if queued."""
    rds = _redis()
    queue = queue_for_type(job_type)
    if not rds or not queue:
        return False
    try:
        rds.lpush(queue, job_id)
        rds.ltrim(queue, 0, MAX_QUEUE_LEN - 1)
        return True
    except Exception:
        return False


def notify_jobs(jobs: list[tuple[str, str]]) -> int:
    """Batch notify (job_type, job_id). Returns count queued."""
    n = 0
    for job_type, job_id in jobs:
        if notify_job_pending(job_type, job_id):
            n += 1
    return n


def status() -> dict[str, Any]:
    rds = _redis()
    out: dict[str, Any] = {
        "enabled": JOB_NOTIFY_ENABLED and bool(REDIS_URL),
        "queues": {WAKE_TRAIN: 0, WAKE_IMAC: 0},
    }
    if not rds:
        return out
    try:
        out["queues"][WAKE_TRAIN] = int(rds.llen(WAKE_TRAIN) or 0)
        out["queues"][WAKE_IMAC] = int(rds.llen(WAKE_IMAC) or 0)
    except Exception:
        pass
    return out
