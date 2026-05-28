"""iMac train_prep — build shard index plan in Postgres for GPU worker."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, "/app")
from train_plan import run_train_prep_job  # noqa: E402


def run_train_prep(payload: dict) -> dict:
    return run_train_prep_job(payload)
