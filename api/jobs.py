"""Postgres job control plane — Phase 0A."""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "")


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def create_train_job() -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO jobs (type, status)
                VALUES ('train', 'pending')
                RETURNING id, type, status, locked_by, error_message,
                          created_at, started_at, finished_at
                """
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_job(row)


def get_job(job_id: UUID) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, type, status, locked_by, error_message,
                       created_at, started_at, finished_at
                FROM jobs WHERE id = %s
                """,
                (job_id,),
            )
            row = cur.fetchone()
    return _row_to_job(row) if row else None


def claim_train_job(worker_id: str) -> dict[str, Any] | None:
    """Atomic claim: one pending train job if no train job is running."""
    with _connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id FROM jobs
                    WHERE type = 'train' AND status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1 FROM jobs
                          WHERE type = 'train' AND status = 'running'
                      )
                    ORDER BY created_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """
                )
                row = cur.fetchone()
                if not row:
                    return None
                job_id = row[0]
                cur.execute(
                    """
                    UPDATE jobs
                    SET status = 'running',
                        locked_by = %s,
                        started_at = COALESCE(started_at, NOW()),
                        error_message = NULL
                    WHERE id = %s
                    RETURNING id, type, status, locked_by, error_message,
                              created_at, started_at, finished_at
                    """,
                    (worker_id, job_id),
                )
                updated = cur.fetchone()
    return _row_to_job(updated)


def reset_stale_running_to_pending() -> int:
    """Recovery after kill -9 (Phase 0A — manual lease; operator or worker startup)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'pending', locked_by = NULL, started_at = NULL
                WHERE type = 'train' AND status = 'running'
                RETURNING id
                """
            )
            n = len(cur.fetchall())
        conn.commit()
    return n


def mark_job_succeeded(job_id: UUID) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'succeeded',
                    finished_at = NOW(),
                    error_message = NULL
                WHERE id = %s AND status = 'running'
                RETURNING id, type, status, locked_by, error_message,
                          created_at, started_at, finished_at
                """,
                (job_id,),
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_job(row) if row else None


def mark_job_failed(job_id: UUID, error_message: str) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE jobs
                SET status = 'failed',
                    finished_at = NOW(),
                    error_message = %s
                WHERE id = %s AND status IN ('running', 'pending')
                RETURNING id, type, status, locked_by, error_message,
                          created_at, started_at, finished_at
                """,
                (error_message[:2000], job_id),
            )
            row = cur.fetchone()
        conn.commit()
    return _row_to_job(row) if row else None


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
    }
