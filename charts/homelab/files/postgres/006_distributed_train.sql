-- Phase 6: distributed train plans (iMac coordinator → GPU executor)

ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
    CHECK (type IN ('train', 'infer', 'ingest', 'embed', 'rag_query', 'train_prep'));

CREATE TABLE IF NOT EXISTS train_plans (
    train_job_id UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    dataset TEXT NOT NULL,
    dataset_version TEXT,
    total_samples INT NOT NULL,
    shard_count INT NOT NULL,
    val_sample_count INT NOT NULL DEFAULT 0,
    seed INT NOT NULL DEFAULT 42,
    meta JSONB,
    ready_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS train_shards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    train_job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    shard_index INT NOT NULL,
    sample_indices JSONB NOT NULL,
    sample_count INT NOT NULL,
    UNIQUE (train_job_id, shard_index)
);

CREATE INDEX IF NOT EXISTS idx_train_shards_job ON train_shards (train_job_id);

CREATE TABLE IF NOT EXISTS train_val_indices (
    train_job_id UUID PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    sample_indices JSONB NOT NULL,
    sample_count INT NOT NULL
);
