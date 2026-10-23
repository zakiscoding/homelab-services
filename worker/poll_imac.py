"""
iMac RAG worker (Phase 2).

Polls the API for ingest/embed/train_prep jobs and runs chunking, embedding (CPU fastembed ->
Qdrant) and shard planning in-process against the same Postgres. Talks to the API only through
/internal/worker/* with ``X-Worker-Key``. Between jobs it blocks on the
``homelab:jobs:wake:imac`` Redis queue for up to WORKER_POLL_SEC.

Env: API_BASE_URL (default http://api:8000), WORKER_API_KEY (required), WORKER_ID,
WORKER_HOST, WORKER_POLL_SEC, plus REDIS_URL / JOB_NOTIFY_ENABLED / DATABASE_URL /
QDRANT_URL used by the imported job modules.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
from contextlib import contextmanager

import httpx

API_BASE = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")
WORKER_ID = os.environ.get("WORKER_ID", "imac-worker")
WORKER_HOST = os.environ.get("WORKER_HOST", socket.gethostname())
POLL_INTERVAL = float(os.environ.get("WORKER_POLL_SEC", "10"))

sys.path.insert(0, "/app")
from job_wake import WAKE_IMAC, wait_for_wake  # noqa: E402
from run_embed import run_embed_job  # noqa: E402
from run_ingest import run_ingest_job  # noqa: E402
from run_train_prep import run_train_prep  # noqa: E402

# iMac job leases are 5 min; renew well before that so long embeds aren't requeued mid-run.
LEASE_RENEW_SEC = 120


def _headers() -> dict[str, str]:
    """Worker auth and identity headers sent on every API call."""
    return {
        "X-Worker-Key": WORKER_API_KEY,
        "X-Worker-Id": WORKER_ID,
        "X-Worker-Host": WORKER_HOST,
    }


def send_heartbeat(client: httpx.Client) -> None:
    """POST /internal/worker/heartbeat; raises on a non-2xx response."""
    client.post(f"{API_BASE}/internal/worker/heartbeat", headers=_headers()).raise_for_status()


def reset_stale(client: httpx.Client) -> None:
    """POST /internal/worker/jobs/reset-stale (startup).

    Requeues only jobs this worker (X-Worker-Id) held when it last stopped, plus any expired
    leases; jobs other workers are running are left alone.
    """
    r = client.post(f"{API_BASE}/internal/worker/jobs/reset-stale", headers=_headers())
    r.raise_for_status()


@contextmanager
def lease_keepalive(job_id: str, every_s: float = LEASE_RENEW_SEC):
    """Renew the job's lease every ``every_s`` seconds in a background thread while the
    with-block runs, so lease expiry can't requeue a job that is still being worked on.
    Uses its own HTTP client (the main one is busy). Renewal errors are logged, not raised."""
    stop = threading.Event()

    def loop() -> None:
        with httpx.Client(timeout=30.0) as c:
            while not stop.wait(every_s):
                try:
                    c.post(f"{API_BASE}/internal/worker/jobs/{job_id}/renew", headers=_headers()).raise_for_status()
                except httpx.HTTPError as e:
                    print(f"[imac-worker] lease renew failed for {job_id}: {e}")

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=5)


def claim_job(client: httpx.Client) -> dict | None:
    """POST /internal/worker/jobs/claim-imac; the job dict (with payload), or None on 204."""
    r = client.post(f"{API_BASE}/internal/worker/jobs/claim-imac", headers=_headers())
    r.raise_for_status()
    if r.status_code == 204:
        return None
    return r.json()


def mark_succeeded(client: httpx.Client, job_id: str) -> None:
    """POST /internal/worker/jobs/{id}/succeeded."""
    client.post(
        f"{API_BASE}/internal/worker/jobs/{job_id}/succeeded",
        headers=_headers(),
    ).raise_for_status()


def mark_failed(client: httpx.Client, job_id: str, msg: str) -> None:
    """POST /internal/worker/jobs/{id}/failed as ``transient`` so the API retries with backoff."""
    client.post(
        f"{API_BASE}/internal/worker/jobs/{job_id}/failed",
        headers=_headers(),
        json={"error_message": msg[:2000], "error_class": "transient"},
    ).raise_for_status()


def run_once(client: httpx.Client) -> bool:
    """Heartbeat, claim one job, run it, and report the outcome. Returns True if a job was claimed.

    Any exception from the job is reported via mark_failed; HTTP errors from heartbeat/claim
    propagate to main().
    """
    send_heartbeat(client)
    job = claim_job(client)
    if not job:
        return False

    job_id = job["id"]
    job_type = job["type"]
    payload = job.get("payload") or {}
    print(f"[imac-worker] claimed {job_type} {job_id}")
    try:
        with lease_keepalive(job_id):
            _run_job(job_type, payload)
        mark_succeeded(client, job_id)
        print(f"[imac-worker] job {job_id} succeeded")
    except Exception as e:
        print(f"[imac-worker] job {job_id} failed: {e}")
        try:
            mark_failed(client, job_id, str(e))
        except Exception as mark_err:
            print(f"[imac-worker] could not mark failed: {mark_err}")
    return True


def _run_job(job_type: str, payload: dict) -> None:
    """Dispatch one claimed iMac job to its handler; raises on failure or unknown type."""
    if job_type == "train_prep":
        result = run_train_prep(payload)
        print(f"[imac-worker] train_prep for {result.get('parent_train_id')} shards={result.get('shard_count')}")
    elif job_type == "ingest":
        doc_id = payload.get("document_id")
        if not doc_id:
            raise ValueError("ingest job missing document_id")
        n = run_ingest_job(doc_id)
        print(f"[imac-worker] ingested {n} chunks for {doc_id}")
    elif job_type == "embed":
        ver = payload.get("collection_version")
        n = run_embed_job(ver)
        print(f"[imac-worker] embedded {n} chunks")
    else:
        raise ValueError(f"unsupported job type: {job_type}")


def main() -> None:
    """Entry point: reset stale jobs once, then loop claiming jobs (``--once`` for a single pass).

    Exits 1 if WORKER_API_KEY is unset. After an idle pass it waits on the iMac wake queue (or
    WORKER_POLL_SEC); after a processed job it polls again almost immediately.
    """
    import argparse

    parser = argparse.ArgumentParser(description="iMac RAG worker (ingest/embed)")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    if not WORKER_API_KEY:
        print("WORKER_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    notify = os.environ.get("JOB_NOTIFY_ENABLED", "true").lower() == "true"
    print(
        f"[imac-worker] id={WORKER_ID} api={API_BASE} poll={POLL_INTERVAL}s notify={notify}"
    )
    with httpx.Client(timeout=120.0) as client:
        reset_stale(client)
        while True:
            try:
                processed = run_once(client)
            except httpx.HTTPError as e:
                print(f"[imac-worker] HTTP error: {e}")
                processed = False
            if args.once:
                break
            if not processed:
                wait_for_wake([WAKE_IMAC], POLL_INTERVAL)
            else:
                time.sleep(0.2)


if __name__ == "__main__":
    main()
