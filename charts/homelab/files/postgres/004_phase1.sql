-- Phase 1: link chat logs to infer jobs for traceability

CREATE TABLE IF NOT EXISTS chat_logs (
    id BIGSERIAL PRIMARY KEY,
    prompt TEXT NOT NULL,
    response TEXT NOT NULL,
    model TEXT NOT NULL,
    duration_ms INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_logs_created_at ON chat_logs (created_at DESC);

ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS job_id UUID REFERENCES jobs(id);

CREATE INDEX IF NOT EXISTS idx_chat_logs_job_id ON chat_logs (job_id) WHERE job_id IS NOT NULL;
