"""Phase 4 — block on Redis wake queues instead of fixed sleep only."""

from __future__ import annotations

import os

import redis

REDIS_URL = os.environ.get("REDIS_URL", "")
JOB_NOTIFY_ENABLED = os.environ.get("JOB_NOTIFY_ENABLED", "true").lower() == "true"
WAKE_TRAIN = "homelab:jobs:wake:train"
WAKE_IMAC = "homelab:jobs:wake:imac"


def wait_for_wake(queues: list[str], timeout_sec: float) -> bool:
    """
    Block up to timeout_sec for a wake signal.
    Returns True if a queue fired; False on timeout or if notify disabled.
    """
    if not JOB_NOTIFY_ENABLED or not REDIS_URL or not queues:
        return False
    try:
        rds = redis.from_url(REDIS_URL, decode_responses=True)
        timeout = max(1, int(timeout_sec))
        return rds.blpop(queues, timeout=timeout) is not None
    except Exception:
        return False
