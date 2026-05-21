-- Phase 1: link chat logs to infer jobs for traceability

ALTER TABLE chat_logs ADD COLUMN IF NOT EXISTS job_id UUID REFERENCES jobs(id);

CREATE INDEX IF NOT EXISTS idx_chat_logs_job_id ON chat_logs (job_id) WHERE job_id IS NOT NULL;
