"""Embed job: vectorize pending chunks into Qdrant."""

from __future__ import annotations

import os
from uuid import UUID

import redis

from rag_engine import embed_texts, upsert_chunks
from rag_store import DEFAULT_COLLECTION_VERSION, get_active_alias, mark_chunks_embedded, pending_chunks
from rag_store import purge_rag_cache_keys

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")


def run_embed_job(collection_version: str | None = None) -> int:
    """Embed up to 128 pending chunks and upsert them into the active Qdrant collection.

    Pending chunks are read for ``collection_version`` (default: the active alias's version) but
    always written to the active alias's Qdrant collection. Chunks are then marked embedded and
    the Redis answer cache for that version is purged. Returns the number of chunks embedded (0
    when none are pending); RuntimeError without an active alias. One call handles one batch;
    remaining chunks wait for the next embed job.
    """
    alias = get_active_alias()
    if not alias:
        raise RuntimeError("no active collection alias")
    ver = collection_version or alias["collection_version"]
    qdrant_name = alias["qdrant_collection_name"]

    chunks = pending_chunks(ver, limit=128)
    if not chunks:
        return 0

    texts = [c["text"] for c in chunks]
    vectors = embed_texts(texts)
    payloads = [
        {
            "text": c["text"],
            "document_id": c["document_id"],
            "chunk_id": c["chunk_id"],
        }
        for c in chunks
    ]
    upsert_chunks(
        qdrant_name,
        [c["chunk_id"] for c in chunks],
        vectors,
        payloads,
    )
    mark_chunks_embedded([UUID(c["chunk_id"]) for c in chunks])

    if REDIS_URL:
        rds = redis.from_url(REDIS_URL, decode_responses=True)
        purge_rag_cache_keys(rds, ver)
    return len(chunks)
