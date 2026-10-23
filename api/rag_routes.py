"""RAG HTTP routes (Phase 2): ingest documents, report the active collection, answer queries.

All routes require ``x-api-key`` (skipped when API_KEY is empty). Ingest only queues work for
the iMac worker; /rag/query runs retrieval (Qdrant) and generation (llm.generate) inline and
caches answers in Redis under ``rag:{collection_version}:{sha256(query)[:16]}`` for one hour.
The cache key ignores ``model`` and ``top_k``; embed jobs purge it for their collection.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any
from uuid import UUID

import redis
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

import llm

from jobs import (
    create_embed_job,
    create_ingest_job,
    create_rag_query_job,
    get_job,
    mark_job_failed,
    mark_job_succeeded,
)
from rag_engine import filter_by_threshold, search_collection
from rag_store import (
    create_document,
    get_active_alias,
    purge_rag_cache_keys,
)

router = APIRouter(prefix="/rag", tags=["rag"])

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
API_KEY = os.environ.get("API_KEY", "")


def _redis_client() -> redis.Redis | None:
    """New Redis client for the answer cache, or None when REDIS_URL is empty."""
    if not REDIS_URL:
        return None
    return redis.from_url(REDIS_URL, decode_responses=True)


def _require_api_key(x_api_key: str | None) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class IngestRequest(BaseModel):
    """Body of POST /rag/ingest. ``auto_embed`` also queues an embed job for new documents."""
    text: str = Field(..., min_length=1)
    source_id: str | None = None
    auto_embed: bool = True


class IngestResponse(BaseModel):
    """POST /rag/ingest result. ``duplicate`` is true when identical text was already ingested."""
    document_id: str
    ingest_job_id: str
    embed_job_id: str | None = None
    duplicate: bool = False


class RagQueryRequest(BaseModel):
    """Body of POST /rag/query; ``top_k`` (1-20) bounds the Qdrant search."""
    query: str = Field(..., min_length=1)
    model: str = "llama3.2:latest"
    top_k: int = Field(default=5, ge=1, le=20)


class RagQueryResponse(BaseModel):
    """POST /rag/query result; ``answer`` is null with ``reason="no_context"`` when no chunk qualifies."""
    answer: str | None
    reason: str | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)
    model: str
    duration_ms: int
    job_id: str | None = None
    cached: bool = False


def _cache_key(collection_version: str, query: str) -> str:
    """Redis cache key for a query against one collection version."""
    qhash = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    return f"rag:{collection_version}:{qhash}"


@router.post("/ingest", response_model=IngestResponse)
def rag_ingest(
    body: IngestRequest,
    x_api_key: str | None = Header(default=None),
) -> IngestResponse:
    """Store a document and queue its ingest (and optionally embed) job.

    Auth: ``x-api-key``. Upserts ``rag_documents`` by content SHA-256 (a repeat keeps the existing
    row and fills ``source_id`` if it was empty). Always queues an ``ingest`` job; queues an
    ``embed`` job only when ``auto_embed`` is true and the document was not already ingested.
    Both wake ``homelab:jobs:wake:imac``. DB errors surface as 500.
    """
    _require_api_key(x_api_key)
    doc = create_document(body.text, body.source_id)
    duplicate = doc["status"] == "ingested"
    ingest_job = create_ingest_job(doc["id"])
    embed_job_id = None
    if body.auto_embed and not duplicate:
        embed_job = create_embed_job()
        embed_job_id = embed_job["id"]
    return IngestResponse(
        document_id=doc["id"],
        ingest_job_id=ingest_job["id"],
        embed_job_id=embed_job_id,
        duplicate=duplicate,
    )


@router.get("/status")
def rag_status(x_api_key: str | None = Header(default=None)) -> dict[str, Any]:
    """Return the ``active`` collection alias. Auth: ``x-api-key``. 503 ``no_active_collection``."""
    _require_api_key(x_api_key)
    alias = get_active_alias()
    if not alias:
        raise HTTPException(status_code=503, detail="no_active_collection")
    return {"active": alias}


@router.post("/query", response_model=RagQueryResponse)
async def rag_query(
    body: RagQueryRequest,
    x_api_key: str | None = Header(default=None),
) -> RagQueryResponse:
    """Answer a question from the active Qdrant collection with citations.

    Auth: ``x-api-key``. Order: Redis cache hit -> return with ``cached=true``. Otherwise create a
    ``rag_query`` job and mark it running (no lease), embed the query on CPU, search Qdrant,
    drop hits under RAG_SCORE_THRESHOLD, then prompt the LLM with the numbered chunks. The
    response (including ``no_context`` answers) is cached for 3600s and the job is marked
    succeeded.
    Errors: 503 ``rag_not_ready`` without an active alias; 503 ``qdrant_error`` on search failure;
    502 on LLM failure. Failures go through mark_job_failed as transient.
    """
    _require_api_key(x_api_key)
    alias = get_active_alias()
    if not alias:
        raise HTTPException(status_code=503, detail="rag_not_ready")

    rds = _redis_client()
    cv = alias["collection_version"]
    cache_key = _cache_key(cv, body.query)
    if rds:
        cached = rds.get(cache_key)
        if cached:
            data = json.loads(cached)
            return RagQueryResponse(**data, cached=True)

    rag_job = None
    try:
        rag_job = create_rag_query_job()
    except Exception:
        rag_job = None

    job_id = rag_job["id"] if rag_job else None
    if job_id:
        from jobs import mark_job_running

        mark_job_running(UUID(job_id))
    started = time.perf_counter()

    try:
        hits = search_collection(alias["qdrant_collection_name"], body.query, top_k=body.top_k)
        hits = filter_by_threshold(hits)
    except Exception as e:
        if job_id:
            mark_job_failed(UUID(job_id), str(e), error_class="transient")
        raise HTTPException(status_code=503, detail=f"qdrant_error: {e}") from e

    if not hits:
        duration_ms = int((time.perf_counter() - started) * 1000)
        out = RagQueryResponse(
            answer=None,
            reason="no_context",
            citations=[],
            model=body.model,
            duration_ms=duration_ms,
            job_id=job_id,
        )
        if job_id:
            mark_job_succeeded(UUID(job_id))
        if rds:
            rds.setex(cache_key, 3600, out.model_dump_json())
        return out

    context = "\n\n".join(f"[{i+1}] {h['text']}" for i, h in enumerate(hits))
    prompt = (
        "Answer using only the context below. If the context is insufficient, say so briefly.\n\n"
        f"Context:\n{context}\n\nQuestion: {body.query}\n\nAnswer:"
    )
    citations = [
        {"chunk_id": h["chunk_id"], "score": h["score"], "text": h["text"][:200]}
        for h in hits
    ]

    try:
        answer, model_used = await llm.generate(body.model, prompt)
    except Exception as e:  # llm.LLMError (upstream 4xx/5xx) or a connection error
        if job_id:
            mark_job_failed(UUID(job_id), str(e), error_class="transient")
        raise HTTPException(status_code=502, detail=str(e)) from e

    duration_ms = int((time.perf_counter() - started) * 1000)
    if job_id:
        mark_job_succeeded(UUID(job_id))

    out = RagQueryResponse(
        answer=answer,
        reason=None,
        citations=citations,
        model=model_used,
        duration_ms=duration_ms,
        job_id=job_id,
    )
    if rds:
        rds.setex(cache_key, 3600, out.model_dump_json())
    return out


def on_embed_job_succeeded(collection_version: str, redis_client: redis.Redis | None) -> None:
    """Purge cached answers for ``collection_version`` (currently unused; run_embed purges directly)."""
    purge_rag_cache_keys(redis_client, collection_version)
