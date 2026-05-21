"""Postgres job control plane — Phase 0A/0B."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "")

JOB_COLUMNS = """
    id, type, status, locked_by, error_message,
    created_at, started_at, finished_at,
    priority, attempt, max_attempts, lease_expires_at, scheduled_at, error_class
"""

LEASE_MINUTES_TRAIN = 15
LEASE_MINUTES_IMAC = 5
BACKOFF_SECONDS = (30, 120, 600)

PRIORITY = {
    "infer": 10,
    "rag_query": 20,
    "ingest": 30,
    "embed": 40,
    "train": 50,
}
IMAC_JOB_TYPES = ("ingest", "embed")


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def _row_to_job(row: tuple) -> dict[str, Any]:
    return {
        "id": str(row[0]),
        "type": row[1],
        "status": row[2],
        "locked_by": row[3],
        "error_message": row[4],
        "created_at": row[5].isoformat() if row[5] else None,
        "started_at": row[6].isoformat() if row[6] else None,
        "finished_at": row[7].isoformat() if row[7] else None,
        "priority": row[8],
        "attempt": row[9],
        "max_attempts": row[10],
        "lease_expires_at": row[11].isoformat() if row[11] else None,
        "scheduled_at": row[12].isoformat() if row[12] else None,
        "error_class": row[13],
    }


def _get_setting(key: str, default: str = "false") -> str:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM system_settings WHERE key = %s", (key,))
            row = cur.fetchone()
    return row[0] if row else default


def create_job(job_type: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    priority = PRIORITY.get(job_type, 50)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO jobs (type, status, priority, payload)
                VALUES (%s, 'pending', %s, %s)
                RETURNING {JOB_COLUMNS.strip()}
                """,
                (job_type, priority, json.dumps(payload) if payload else None),
            )
            row = cur.fetchone()
        conn.commit()
    job = _row_to_job(row)
    job["payload"] = payload
    return job


def create_ingest_job(document_id: str) -> dict[str, Any]:
    return create_job("ingest", {"document_id": document_id})


def create_embed_job(collection_version: str | None = None) -> dict[str, Any]:
    ver = collection_version or "v1_minilm_l6"
    return create_job("embed", {"collection_version": ver})


def create_rag_query_job() -> dict[str, Any]:
    return create_job("rag_query")


def create_train_job(
    *,
    dataset_version: str | None = None,
    model_version: str | None = None,
    base_model: str | None = None,
    hyperparams: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if _get_setting("training_paused") == "true":
        raise RuntimeError("training_paused is true")
    payload: dict[str, Any] = {}
    if dataset_version:
        payload["dataset_version"] = dataset_version
    if model_version:
        payload["model_version"] = model_version
    if base_model:
        payload["base_model"] = base_model
    if hyperparams:
        payload.update(hyperparams)
    return create_job("train", payload or None)


def create_infer_job() -> dict[str, Any]:
    if _get_setting("allow_infer_during_train") != "true":
        with _connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM jobs WHERE type = 'train' AND status = 'running' LIMIT 1"
                )
                if cur.fetchone():
                    raise RuntimeError("gpu_busy: train job running")
    return create_job("infer")


def get_job(job_id: UUID) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {JOB_COLUMNS} FROM jobs WHERE id = %s",
                (job_id,),
            )
            row = cur.fetchone()
    return _row_to_job(row) if row else None


def _expire_leases_for_types(job_types: tuple[str, ...], lease_minutes: int) -> int:
    n = 0
    with _connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, attempt, max_attempts FROM jobs
                    WHERE type = ANY(%s) AND status = 'running'
                      AND lease_expires_at IS NOT NULL
                      AND lease_expires_at < NOW()
                    FOR UPDATE
                    """,
                    (list(job_types),),
                )
                rows = cur.fetchall()
                for job_id, attempt, max_attempts in rows:
                    if attempt + 1 < max_attempts:
                        cur.execute(
                            """
                            UPDATE jobs
                            SET status = 'pending',
                                locked_by = NULL,
                                started_at = NULL,
                                lease_expires_at = NULL,
                                attempt = attempt + 1,
                                scheduled_at = NOW() + make_interval(secs => %s),
                                error_message = 'lease_expired',
                                error_class = 'transient'
                            WHERE id = %s
                            """,
                            (BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)], job_id),
                        )
                    else:
                        cur.execute(
                            """
                            UPDATE jobs
                            SET status = 'failed',
                                finished_at = NOW(),
                                error_message = 'lease_expired_max_attempts',
                                error_class = 'transient'
                            WHERE id = %s
                            """,
                            (job_id,),
                        )
                    n += 1
        conn.commit()
    return n


def expire_stale_leases() -> int:
    """Requeue expired running jobs (train + iMac types) or mark failed."""
    n = _expire_leases_for_types(("train",), LEASE_MINUTES_TRAIN)
    n += _expire_leases_for_types(IMAC_JOB_TYPES, LEASE_MINUTES_IMAC)
    return n


def claim_train_job(worker_id: str) -> dict[str, Any] | None:
    _expire_leases_for_types(("train",), LEASE_MINUTES_TRAIN)
    with _connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM jobs
                    WHERE type = 'train' AND status = 'pending'
                      AND scheduled_at <= NOW()
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs
                          WHERE type = 'train' AND status = 'running'
                      )
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
                if not row:
                    return None
                job_id = row[0]
                cur.execute(
                    f"""
                    UPDATE jobs
                    SET status = 'running',
                        locked_by = %s,
                        started_at = COALESCE(started_at, NOW()),
                        lease_expires_at = NOW() + INTERVAL '{LEASE_MINUTES_TRAIN} minutes',
                        error_message = NULL,
                        error_class = NULL
                    WHERE id = %s
                    RETURNING {JOB_COLUMNS}
                    """,
                    (worker_id, job_id),
                )
                updated = cur.fetchone()
                cur.execute("SELECT payload FROM jobs WHERE id = %s", (job_id,))
                payload_row = cur.fetchone()
    job = _row_to_job(updated)
    if payload_row and payload_row[0]:
        job["payload"] = (
            payload_row[0] if isinstance(payload_row[0], dict) else json.loads(payload_row[0])
        )
    else:
        job["payload"] = {}
    return job


def reset_stale_running_to_pending() -> int:
    """Force-reset all running train + iMac jobs (operator / worker startup)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'pending', locked_by = NULL,
                    started_at = NULL, lease_expires_at = NULL
                WHERE type IN ('train', 'ingest', 'embed') AND status = 'running'
                RETURNING id
                """
            )
            n = len(cur.fetchall())
        conn.commit()
    return n


def claim_imac_job(worker_id: str) -> dict[str, Any] | None:
    """Claim one ingest or embed job (max 1 iMac job running at a time)."""
    _expire_leases_for_types(IMAC_JOB_TYPES, LEASE_MINUTES_IMAC)
    with _connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, type FROM jobs
                    WHERE type IN ('ingest', 'embed') AND status = 'pending'
                      AND scheduled_at <= NOW()
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs
                          WHERE type IN ('ingest', 'embed') AND status = 'running'
                      )
                    ORDER BY priority ASC, created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
                if not row:
                    return None
                job_id, _job_type = row[0], row[1]
                cur.execute(
                    f"""
                    UPDATE jobs
                    SET status = 'running',
                        locked_by = %s,
                        started_at = COALESCE(started_at, NOW()),
                        lease_expires_at = NOW() + INTERVAL '{LEASE_MINUTES_IMAC} minutes',
                        error_message = NULL,
                        error_class = NULL
                    WHERE id = %s
                    RETURNING {JOB_COLUMNS}
                    """,
                    (worker_id, job_id),
                )
                updated = cur.fetchone()
                cur.execute("SELECT payload FROM jobs WHERE id = %s", (job_id,))
                payload_row = cur.fetchone()
    job = _row_to_job(updated)
    if payload_row and payload_row[0]:
        job["payload"] = payload_row[0] if isinstance(payload_row[0], dict) else json.loads(payload_row[0])
    else:
        job["payload"] = {}
    return job


def mark_job_running(job_id: UUID) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs SET status = 'running', started_at = NOW()
                WHERE id = %s AND status = 'pending'
                """,
                (job_id,),
            )
        conn.commit()


def mark_job_succeeded(job_id: UUID) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE jobs
                SET status = 'succeeded',
                    finished_at = NOW(),
                    lease_expires_at = NULL,
                    error_message = NULL,
                    error_class = NULL
                WHERE id = %s AND status = 'running'
                RETURNING {JOB_COLUMNS}
                """,
                (job_id,),
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_job(row) if row else None


def mark_job_failed(
    job_id: UUID,
    error_message: str,
    *,
    error_class: str = "transient",
    retry: bool | None = None,
) -> dict[str, Any] | None:
    if retry is None:
        retry = error_class == "transient"

    with _connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    f"SELECT attempt, max_attempts, status FROM jobs WHERE id = %s",
                    (job_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                attempt, max_attempts, status = row
                if status not in ("running", "pending"):
                    return None

                if retry and error_class == "transient" and attempt + 1 < max_attempts:
                    backoff = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                    cur.execute(
                        f"""
                        UPDATE jobs
                        SET status = 'pending',
                            locked_by = NULL,
                            started_at = NULL,
                            finished_at = NULL,
                            lease_expires_at = NULL,
                            attempt = attempt + 1,
                            scheduled_at = NOW() + make_interval(secs => %s),
                            error_message = %s,
                            error_class = %s
                        WHERE id = %s
                        RETURNING {JOB_COLUMNS}
                        """,
                        (backoff, error_message[:2000], error_class, job_id),
                    )
                else:
                    cur.execute(
                        f"""
                        UPDATE jobs
                        SET status = 'failed',
                            finished_at = NOW(),
                            lease_expires_at = NULL,
                            error_message = %s,
                            error_class = %s
                        WHERE id = %s
                        RETURNING {JOB_COLUMNS}
                        """,
                        (error_message[:2000], error_class, job_id),
                    )
                out = cur.fetchone()
        conn.commit()
    return _row_to_job(out) if out else None


def record_worker_heartbeat(worker_id: str, host: str | None = None) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO worker_heartbeats (worker_id, host, last_seen_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (worker_id) DO UPDATE
                SET host = EXCLUDED.host, last_seen_at = NOW()
                """,
                (worker_id, host or worker_id),
            )
        conn.commit()


def get_system_status(gpu_url: str) -> dict[str, Any]:
    """Aggregate situational awareness for GET /system/status."""
    now = datetime.now(timezone.utc).isoformat()
    status: dict[str, Any] = {
        "timestamp": now,
        "control_plane": "postgres",
        "jobs": {"pending": 0, "running": 0, "failed_last_24h": 0, "by_type": {}},
        "queue_depth": {},
        "workers": {},
        "flags": {
            "training_paused": _get_setting("training_paused") == "true",
            "allow_infer_during_train": _get_setting("allow_infer_during_train") == "true",
        },
        "recent_failures": [],
    }

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT type, status, COUNT(*) FROM jobs
                GROUP BY type, status
                """
            )
            for job_type, job_status, count in cur.fetchall():
                status["jobs"]["by_type"].setdefault(job_type, {})
                status["jobs"]["by_type"][job_type][job_status] = count
                if job_status == "pending":
                    status["jobs"]["pending"] += count
                    status["queue_depth"][job_type] = count
                elif job_status == "running":
                    status["jobs"]["running"] += count

            cur.execute(
                """
                SELECT id::text, type, error_message, finished_at
                FROM jobs
                WHERE status = 'failed' AND finished_at > NOW() - INTERVAL '24 hours'
                ORDER BY finished_at DESC
                LIMIT 10
                """
            )
            for jid, jtype, err, finished in cur.fetchall():
                status["recent_failures"].append(
                    {
                        "job_id": jid,
                        "type": jtype,
                        "error": (err or "")[:200],
                        "at": finished.isoformat() if finished else None,
                    }
                )
            status["jobs"]["failed_last_24h"] = len(status["recent_failures"])

            cur.execute(
                "SELECT worker_id, host, last_seen_at FROM worker_heartbeats"
            )
            for wid, host, seen in cur.fetchall():
                status["workers"][wid] = {
                    "host": host,
                    "last_seen_at": seen.isoformat() if seen else None,
                }

    status["gpu"] = {"reachable": False, "ollama_up": False, "train_slot_free": True}
    try:
        import httpx

        r = httpx.get(f"{gpu_url}/api/tags", timeout=5.0)
        status["gpu"]["reachable"] = True
        status["gpu"]["ollama_up"] = r.status_code == 200
    except Exception:
        pass
    status["gpu"]["train_slot_free"] = (
        status["jobs"]["by_type"].get("train", {}).get("running", 0) == 0
    )

    status["rag"] = {
        "health": "unknown",
        "qdrant_ready": False,
        "active_collection_version": None,
        "embedding_model_id": EMBEDDING_MODEL_ID,
        "pending_chunks": 0,
    }
    try:
        import httpx

        from rag_engine import EMBEDDING_MODEL_ID
        from rag_store import count_pending_chunks, get_active_alias

        r = httpx.get("http://qdrant:6333/readyz", timeout=3.0)
        status["rag"]["qdrant_ready"] = r.status_code == 200
        status["rag"]["health"] = "ok" if r.status_code == 200 else "degraded"
        alias = get_active_alias()
        if alias:
            status["rag"]["active_collection_version"] = alias["collection_version"]
            status["rag"]["embedding_model_id"] = alias["embedding_model_id"]
            status["rag"]["pending_chunks"] = count_pending_chunks(alias["collection_version"])
    except Exception:
        status["rag"]["health"] = "degraded"

    return status


def create_experiment_for_job(
    job_id: UUID,
    *,
    run_id: str | None = None,
    dataset_version: str | None = None,
    model_version: str | None = None,
    git_commit: str | None = None,
    hyperparams: dict | None = None,
    output_path: str | None = None,
) -> dict[str, Any]:
    rid = run_id or f"exp-{datetime.now(timezone.utc).strftime('%Y%m%d')}-{uuid4().hex[:6]}"
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO experiments (
                    run_id, job_id, dataset_version, model_version,
                    git_commit, hyperparams, output_path
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, run_id, job_id, dataset_version, model_version,
                          git_commit, output_path, created_at
                """,
                (
                    rid,
                    job_id,
                    dataset_version,
                    model_version,
                    git_commit,
                    json.dumps(hyperparams or {}),
                    output_path,
                ),
            )
            row = cur.fetchone()
        conn.commit()
    return {
        "id": str(row[0]),
        "run_id": row[1],
        "job_id": str(row[2]) if row[2] else None,
        "dataset_version": row[3],
        "model_version": row[4],
        "git_commit": row[5],
        "output_path": row[6],
        "created_at": row[7].isoformat() if row[7] else None,
    }
