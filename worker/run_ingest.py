"""Ingest job: chunk document text into rag_chunks."""

from __future__ import annotations

from uuid import UUID

from rag_store import DEFAULT_COLLECTION_VERSION, run_ingest_for_document


def run_ingest_job(document_id: str, collection_version: str | None = None) -> int:
    ver = collection_version or DEFAULT_COLLECTION_VERSION
    return run_ingest_for_document(UUID(document_id), ver)
