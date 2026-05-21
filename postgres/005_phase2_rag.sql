-- Phase 2: RAG documents, chunks, collection aliases, job payloads

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS payload JSONB;

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
    CHECK (type IN ('train', 'infer', 'ingest', 'embed', 'rag_query'));

CREATE TABLE IF NOT EXISTS rag_documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id TEXT,
    raw_text TEXT NOT NULL,
    content_sha256 TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'ingested', 'failed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    chunk_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES rag_documents(id) ON DELETE CASCADE,
    chunk_index INT NOT NULL,
    text TEXT NOT NULL,
    collection_version TEXT NOT NULL DEFAULT 'v1_minilm_l6',
    embedded_at TIMESTAMPTZ,
    UNIQUE (document_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_embed_pending
    ON rag_chunks (collection_version) WHERE embedded_at IS NULL;

CREATE TABLE IF NOT EXISTS collection_aliases (
    name TEXT PRIMARY KEY,
    collection_version TEXT NOT NULL,
    qdrant_collection_name TEXT NOT NULL,
    embedding_model_id TEXT NOT NULL,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO collection_aliases (name, collection_version, qdrant_collection_name, embedding_model_id)
VALUES ('active', 'v1_minilm_l6', 'coll_v1_minilm_l6', 'minilm-l6-v1')
ON CONFLICT (name) DO NOTHING;
