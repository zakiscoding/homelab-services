"""iMac train_prep — build shard index plan in Postgres for GPU worker."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/app")
from train_plan import run_train_prep_job  # noqa: E402


def run_train_prep(payload: dict) -> dict:
    """Run a ``train_prep`` payload via train_plan.run_train_prep_job.

    api/train_plan.py is copied into the worker image at /app.
    """
    return run_train_prep_job(payload)
