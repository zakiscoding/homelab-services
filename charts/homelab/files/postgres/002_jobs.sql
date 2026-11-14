-- Phase 0A: minimal job control plane (run on existing DB via scripts/migrate-jobs.sh)
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    type TEXT NOT NULL DEFAULT 'train',
    status TEXT NOT NULL DEFAULT 'pending',
    locked_by TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    CONSTRAINT jobs_type_check CHECK (type IN ('train')),
    CONSTRAINT jobs_status_check CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed')
    )
);

CREATE INDEX IF NOT EXISTS idx_jobs_status_type_created
    ON jobs (status, type, created_at);
