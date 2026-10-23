"""Postgres helpers for RAG documents, chunks, and collection aliases."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any
from uuid import UUID

import psycopg

from rag_engine import EMBEDDING_MODEL_ID

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DEFAULT_COLLECTION_VERSION = "v1_minilm_l6"


def _connect():
    """Open a new psycopg connection; RuntimeError if DATABASE_URL is unset."""
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")
    return psycopg.connect(DATABASE_URL)


def sha256_text(text: str) -> str:
    """Hex SHA-256 of UTF-8 text; used to deduplicate documents."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_text(text: str, *, size: int = 400, overlap: int = 50) -> list[str]:
    """Split stripped text into ``size``-char windows overlapping by ``overlap`` chars."""
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return chunks


def get_active_alias() -> dict[str, Any] | None:
    """Return the ``collection_aliases`` row named ``active``, or None.

    It names the collection version, Qdrant collection and embedding model that queries use.
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT name, collection_version, qdrant_collection_name, embedding_model_id, activated_at
                FROM collection_aliases WHERE name = 'active'
                """
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "name": row[0],
        "collection_version": row[1],
        "qdrant_collection_name": row[2],
        "embedding_model_id": row[3],
        "activated_at": row[4].isoformat() if row[4] else None,
    }


def create_document(text: str, source_id: str | None) -> dict[str, Any]:
    """Upsert a ``rag_documents`` row keyed by content SHA-256 and return it.

    A new document starts ``pending``. For an existing one the status is unchanged (callers treat
    ``ingested`` as a duplicate) and ``source_id`` is only filled when previously null.
    """
    digest = sha256_text(text)
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO rag_documents (source_id, raw_text, content_sha256, status)
                VALUES (%s, %s, %s, 'pending')
                ON CONFLICT (content_sha256) DO UPDATE
                    SET source_id = COALESCE(EXCLUDED.source_id, rag_documents.source_id)
                RETURNING id, source_id, status, content_sha256, created_at
                """,
                (source_id, text, digest),
            )
            row = cur.fetchone()
        conn.commit()
    return {
        "id": str(row[0]),
        "source_id": row[1],
        "status": row[2],
        "content_sha256": row[3],
        "created_at": row[4].isoformat() if row[4] else None,
    }


def get_document(document_id: UUID) -> dict[str, Any] | None:
    """Return a document with its raw text, or None."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, source_id, raw_text, status FROM rag_documents WHERE id = %s",
                (document_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": str(row[0]),
        "source_id": row[1],
        "raw_text": row[2],
        "status": row[3],
    }


def run_ingest_for_document(document_id: UUID, collection_version: str) -> int:
    """Re-chunk one document into ``rag_chunks`` for ``collection_version``.

    In one transaction: delete the document's existing chunks, insert the new ones (unembedded)
    and set the document to ``ingested``. Returns the chunk count; ValueError if the document is
    missing.
    """
    doc = get_document(document_id)
    if not doc:
        raise ValueError("document not found")
    pieces = chunk_text(doc["raw_text"])
    with _connect() as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM rag_chunks WHERE document_id = %s",
                    (document_id,),
                )
                for i, piece in enumerate(pieces):
                    cur.execute(
                        """
                        INSERT INTO rag_chunks (document_id, chunk_index, text, collection_version)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (document_id, i, piece, collection_version),
                    )
                cur.execute(
                    "UPDATE rag_documents SET status = 'ingested' WHERE id = %s",
                    (document_id,),
                )
        conn.commit()
    return len(pieces)


def pending_chunks(collection_version: str, *, limit: int = 64) -> list[dict[str, Any]]:
    """Up to ``limit`` not-yet-embedded chunks of ``collection_version``, in document order."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT chunk_id, document_id, text
                FROM rag_chunks
                WHERE collection_version = %s AND embedded_at IS NULL
                ORDER BY document_id, chunk_index
                LIMIT %s
                """,
                (collection_version, limit),
            )
            rows = cur.fetchall()
    return [
        {"chunk_id": str(r[0]), "document_id": str(r[1]), "text": r[2]}
        for r in rows
    ]


def mark_chunks_embedded(chunk_ids: list[UUID]) -> None:
    """Stamp ``embedded_at = NOW()`` on the given chunk ids."""
    if not chunk_ids:
        return
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE rag_chunks SET embedded_at = NOW() WHERE chunk_id = ANY(%s)",
                (chunk_ids,),
            )
        conn.commit()


def count_pending_chunks(collection_version: str) -> int:
    """Number of chunks in ``collection_version`` still waiting to be embedded."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM rag_chunks
                WHERE collection_version = %s AND embedded_at IS NULL
                """,
                (collection_version,),
            )
            return int(cur.fetchone()[0])


def purge_rag_cache_keys(redis_client, collection_version: str) -> int:
    """Delete cached answers ``rag:{collection_version}:*`` via SCAN; returns the count (0 without Redis)."""
    if not redis_client:
        return 0
    pattern = f"rag:{collection_version}:*"
    n = 0
    for key in redis_client.scan_iter(match=pattern, count=200):
        redis_client.delete(key)
        n += 1
    return n
