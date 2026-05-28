#!/usr/bin/env bash
# Create canonical ML-STORE tree on iMac 1TB (hybrid storage — Phase 0B).
set -euo pipefail
BASE="${ML_STORE_BASE:-/Volumes/1tb drive/ML-STORE}"
mkdir -p \
  "$BASE/datasets" \
  "$BASE/rag/raw" \
  "$BASE/rag/chunks" \
  "$BASE/embeddings" \
  "$BASE/mlflow" \
  "$BASE/models/base" \
  "$BASE/models/finetuned" \
  "$BASE/models/adapters" \
  "$BASE/logs"
echo "ML-STORE ready at $BASE"
