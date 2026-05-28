"""
iMac RAG worker — Phase 2.

Polls API for ingest/embed jobs, runs chunking + embedding.
"""

from __future__ import annotations

import os
import socket
import sys
import time

import httpx

API_BASE = os.environ.get("API_BASE_URL", "http://api:8000").rstrip("/")
WORKER_API_KEY = os.environ.get("WORKER_API_KEY", "")
WORKER_ID = os.environ.get("WORKER_ID", "imac-worker")
WORKER_HOST = os.environ.get("WORKER_HOST", socket.gethostname())
POLL_INTERVAL = float(os.environ.get("WORKER_POLL_SEC", "10"))

sys.path.insert(0, "/app")
from job_wake import WAKE_IMAC, wait_for_wake  # noqa: E402
from run_embed import run_embed_job  # noqa: E402
from run_train_prep import run_train_prep  # noqa: E402


def _headers() -> dict[str, str]:
    return {
        "X-Worker-Key": WORKER_API_KEY,
        "X-Worker-Id": WORKER_ID,
        "X-Worker-Host": WORKER_HOST,
    }


def send_heartbeat(client: httpx.Client) -> None:
    client.post(f"{API_BASE}/internal/worker/heartbeat", headers=_headers()).raise_for_status()


def reset_stale(client: httpx.Client) -> None:
    r = client.post(f"{API_BASE}/internal/worker/jobs/reset-stale", headers=_headers())
    r.raise_for_status()


def claim_job(client: httpx.Client) -> dict | None:
    r = client.post(f"{API_BASE}/internal/worker/jobs/claim-imac", headers=_headers())
    r.raise_for_status()
    if r.status_code == 204:
        return None
    return r.json()


def mark_succeeded(client: httpx.Client, job_id: str) -> None:
    client.post(
        f"{API_BASE}/internal/worker/jobs/{job_id}/succeeded",
        headers=_headers(),
    ).raise_for_status()


def mark_failed(client: httpx.Client, job_id: str, msg: str) -> None:
    client.post(
        f"{API_BASE}/internal/worker/jobs/{job_id}/failed",
        headers=_headers(),
        json={"error_message": msg[:2000], "error_class": "transient"},
    ).raise_for_status()


def run_once(client: httpx.Client) -> bool:
    send_heartbeat(client)
    job = claim_job(client)
    if not job:
        return False

    job_id = job["id"]
    job_type = job["type"]
    payload = job.get("payload") or {}
    print(f"[imac-worker] claimed {job_type} {job_id}")
    try:
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
        mark_succeeded(client, job_id)
        print(f"[imac-worker] job {job_id} succeeded")
    except Exception as e:
        print(f"[imac-worker] job {job_id} failed: {e}")
        try:
            mark_failed(client, job_id, str(e))
        except Exception as mark_err:
            print(f"[imac-worker] could not mark failed: {mark_err}")
    return True


def main() -> None:
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
