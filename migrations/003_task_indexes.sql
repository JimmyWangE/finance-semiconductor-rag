CREATE INDEX IF NOT EXISTS task_jobs_claim ON task_jobs(status,run_at,priority DESC,created_at);
CREATE INDEX IF NOT EXISTS task_jobs_owner ON task_jobs(owner_id,created_at DESC);
CREATE INDEX IF NOT EXISTS task_jobs_lease ON task_jobs(status,lease_expires_at);
CREATE INDEX IF NOT EXISTS task_events_stream ON task_events(task_id,id);
