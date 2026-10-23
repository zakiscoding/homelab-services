"""RAG embeddings (CPU) and Qdrant search — Phase 2."""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
EMBEDDING_MODEL_ID = os.environ.get("EMBEDDING_MODEL_ID", "minilm-l6-v1")
VECTOR_SIZE = 384
SCORE_THRESHOLD = float(os.environ.get("RAG_SCORE_THRESHOLD", "0.35"))


@lru_cache(maxsize=1)
def _embedder():
    """Lazily load the fastembed all-MiniLM-L6-v2 model (384-dim, CPU) once per process."""
    from fastembed import TextEmbedding

    return TextEmbedding(model_name="sentence-transformers/all-MiniLM-L6-v2")


@lru_cache(maxsize=1)
def qdrant_client() -> QdrantClient:
    """Process-wide Qdrant client for QDRANT_URL."""
    return QdrantClient(url=QDRANT_URL, timeout=30)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts with the CPU MiniLM model; returns one 384-float vector per text."""
    if not texts:
        return []
    model = _embedder()
    return [list(vec) for vec in model.embed(texts)]


def ensure_collection(collection_name: str) -> None:
    """Create ``collection_name`` in Qdrant (384-dim, cosine) if it does not exist."""
    client = qdrant_client()
    names = {c.name for c in client.get_collections().collections}
    if collection_name in names:
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=qm.VectorParams(size=VECTOR_SIZE, distance=qm.Distance.COSINE),
    )


def upsert_chunks(
    collection_name: str,
    chunk_ids: list[str],
    vectors: list[list[float]],
    payloads: list[dict[str, Any]],
) -> None:
    """Upsert chunk vectors into Qdrant (point id = chunk id), creating the collection if needed."""
    ensure_collection(collection_name)
    points = [
        qm.PointStruct(id=cid, vector=vec, payload=pl)
        for cid, vec, pl in zip(chunk_ids, vectors, payloads, strict=True)
    ]
    qdrant_client().upsert(collection_name=collection_name, points=points)


def search_collection(
    collection_name: str,
    query: str,
    *,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Embed ``query`` and return the ``top_k`` nearest chunks.

    Each hit is a dict with chunk_id, text, document_id and cosine score. A missing
    collection is created first, so it returns no hits instead of erroring.
    """
    ensure_collection(collection_name)
    qvec = embed_texts([query])[0]
    hits = qdrant_client().search(
        collection_name=collection_name,
        query_vector=qvec,
        limit=top_k,
        with_payload=True,
    )
    out: list[dict[str, Any]] = []
    for h in hits:
        score = float(h.score) if h.score is not None else 0.0
        pl = h.payload or {}
        out.append(
            {
                "chunk_id": str(h.id),
                "text": pl.get("text", ""),
                "document_id": pl.get("document_id"),
                "score": score,
            }
        )
    return out


def filter_by_threshold(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep hits whose cosine score is at least RAG_SCORE_THRESHOLD (default 0.35)."""
    return [h for h in hits if h.get("score", 0) >= SCORE_THRESHOLD]
