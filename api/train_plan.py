"""Distributed train plan — iMac shards indices, GPU executes (Postgres SSOT)."""

from __future__ import annotations

import json
import os
import random
from typing import Any
from uuid import UUID

import psycopg

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DATASET_TRAIN_SIZES: dict[str, int] = {
    "cifar10": 50_000,
    "fer2013": 28_709,
}


def _connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def resolve_train_sample_count(dataset: str, dataset_version: str | None, payload: dict) -> int:
    if payload.get("total_samples"):
        return int(payload["total_samples"])
    key = (dataset or "cifar10").lower()
    if key in DATASET_TRAIN_SIZES:
        return DATASET_TRAIN_SIZES[key]
    raise ValueError(
        f"Unknown dataset size for '{dataset}'. Set total_samples in job payload or use cifar10/fer2013."
    )


def build_shard_plan(
    *,
    total_samples: int,
    shard_count: int,
    val_ratio: float = 0.1,
    seed: int = 42,
) -> tuple[list[list[int]], list[int]]:
    shard_count = max(1, min(shard_count, total_samples))
    indices = list(range(total_samples))
    rng = random.Random(seed)
    rng.shuffle(indices)
    val_n = int(total_samples * val_ratio) if val_ratio > 0 else 0
    val_indices = indices[:val_n]
    train_indices = indices[val_n:]
    if not train_indices:
        train_indices = val_indices[: max(1, len(val_indices) // 2)]
        val_indices = val_indices[len(train_indices) :]

    shards: list[list[int]] = [[] for _ in range(shard_count)]
    for i, idx in enumerate(train_indices):
        shards[i % shard_count].append(idx)
    shards = [s for s in shards if s]
    return shards, val_indices


def save_train_plan(
    train_job_id: UUID,
    *,
    dataset: str,
    dataset_version: str | None,
    shards: list[list[int]],
    val_indices: list[int],
    seed: int,
    meta: dict[str, Any] | None = None,
) -> None:
    total = sum(len(s) for s in shards) + len(val_indices)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO train_plans (
                    train_job_id, dataset, dataset_version,
                    total_samples, shard_count, val_sample_count, seed, meta, ready_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (train_job_id) DO UPDATE SET
                    ready_at = NOW(),
                    shard_count = EXCLUDED.shard_count,
                    total_samples = EXCLUDED.total_samples
                """,
                (
                    train_job_id,
                    dataset,
                    dataset_version,
                    total,
                    len(shards),
                    len(val_indices),
                    seed,
                    json.dumps(meta or {}),
                ),
            )
            cur.execute("DELETE FROM train_shards WHERE train_job_id = %s", (train_job_id,))
            for i, shard in enumerate(shards):
                cur.execute(
                    """
                    INSERT INTO train_shards (train_job_id, shard_index, sample_indices, sample_count)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (train_job_id, i, json.dumps(shard), len(shard)),
                )
            cur.execute(
                """
                INSERT INTO train_val_indices (train_job_id, sample_indices, sample_count)
                VALUES (%s, %s, %s)
                ON CONFLICT (train_job_id) DO UPDATE SET
                    sample_indices = EXCLUDED.sample_indices,
                    sample_count = EXCLUDED.sample_count
                """,
                (train_job_id, json.dumps(val_indices), len(val_indices)),
            )
        conn.commit()
    try:
        from job_notify import notify_job_pending

        notify_job_pending("train", str(train_job_id))
    except Exception:
        pass


def get_train_plan(train_job_id: UUID) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT dataset, dataset_version, total_samples, shard_count,
                       val_sample_count, seed, meta, ready_at
                FROM train_plans WHERE train_job_id = %s
                """,
                (train_job_id,),
            )
            plan_row = cur.fetchone()
            if not plan_row or not plan_row[7]:
                return None
            cur.execute(
                """
                SELECT shard_index, sample_indices, sample_count
                FROM train_shards WHERE train_job_id = %s ORDER BY shard_index
                """,
                (train_job_id,),
            )
            shard_rows = cur.fetchall()
            cur.execute(
                "SELECT sample_indices, sample_count FROM train_val_indices WHERE train_job_id = %s",
                (train_job_id,),
            )
            val_row = cur.fetchone()
    meta = plan_row[6]
    if isinstance(meta, str):
        meta = json.loads(meta)
    shards = []
    for idx, indices, count in shard_rows:
        if isinstance(indices, str):
            indices = json.loads(indices)
        shards.append({"shard_index": idx, "indices": indices, "sample_count": count})
    val_indices = []
    if val_row:
        val_indices = val_row[0]
        if isinstance(val_indices, str):
            val_indices = json.loads(val_indices)
    return {
        "train_job_id": str(train_job_id),
        "dataset": plan_row[0],
        "dataset_version": plan_row[1],
        "total_samples": plan_row[2],
        "shard_count": plan_row[3],
        "val_sample_count": plan_row[4],
        "seed": plan_row[5],
        "meta": meta or {},
        "ready_at": plan_row[7].isoformat() if plan_row[7] else None,
        "shards": shards,
        "val_indices": val_indices,
    }


def run_train_prep_job(payload: dict[str, Any]) -> dict[str, Any]:
    parent = payload.get("parent_train_id")
    if not parent:
        raise ValueError("train_prep missing parent_train_id")
    train_job_id = UUID(str(parent))
    dataset = (payload.get("dataset") or payload.get("cnn_dataset") or "cifar10").lower()
    dataset_version = payload.get("dataset_version")
    shard_count = int(payload.get("shard_count", 8))
    seed = int(payload.get("seed", 42))
    val_ratio = float(payload.get("val_ratio", 0.1))
    total = resolve_train_sample_count(dataset, dataset_version, payload)
    shards, val_indices = build_shard_plan(
        total_samples=total,
        shard_count=shard_count,
        val_ratio=val_ratio,
        seed=seed,
    )
    save_train_plan(
        train_job_id,
        dataset=dataset,
        dataset_version=dataset_version,
        shards=shards,
        val_indices=val_indices,
        seed=seed,
        meta={
            "coordinator": "imac",
            "executor": "gpu",
            "shard_count": len(shards),
            "train_mode": payload.get("train_mode"),
        },
    )
    return {
        "parent_train_id": str(train_job_id),
        "shard_count": len(shards),
        "val_samples": len(val_indices),
        "total_samples": total,
    }
