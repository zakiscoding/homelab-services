-- Phase 0B: leases, retry, priority, heartbeats, experiments, system flags

ALTER TABLE jobs ADD COLUMN IF NOT EXISTS priority INT NOT NULL DEFAULT 50;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS attempt INT NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS max_attempts INT NOT NULL DEFAULT 3;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS error_class TEXT;

-- Allow infer jobs (sync API executor)
ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_type_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_type_check
    CHECK (type IN ('train', 'infer'));

CREATE INDEX IF NOT EXISTS idx_jobs_scheduled
    ON jobs (status, type, scheduled_at, priority);

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_id TEXT PRIMARY KEY,
    host TEXT,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS system_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

INSERT INTO system_settings (key, value) VALUES
    ('training_paused', 'false'),
    ('allow_infer_during_train', 'false')
ON CONFLICT (key) DO NOTHING;

CREATE TABLE IF NOT EXISTS experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id TEXT UNIQUE NOT NULL,
    job_id UUID REFERENCES jobs(id),
    dataset_version TEXT,
    model_version TEXT,
    git_commit TEXT,
    hyperparams JSONB,
    output_path TEXT,
    manifest_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
