CREATE TABLE IF NOT EXISTS task_jobs (
  id uuid PRIMARY KEY,
  type text NOT NULL,
  owner_id text NOT NULL,
  status text NOT NULL CHECK (status IN ('queued','running','retry_wait','succeeded','failed','cancelled')),
  payload jsonb NOT NULL,
  result jsonb,
  error jsonb,
  idempotency_key text,
  priority smallint NOT NULL DEFAULT 0,
  run_at timestamptz NOT NULL DEFAULT now(),
  attempt integer NOT NULL DEFAULT 0,
  max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
  lease_owner text,
  lease_expires_at timestamptz,
  cancel_requested_at timestamptz,
  parent_task_id uuid REFERENCES task_jobs(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  started_at timestamptz,
  finished_at timestamptz,
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(owner_id,type,idempotency_key)
);

CREATE TABLE IF NOT EXISTS task_events (
  id bigserial PRIMARY KEY,
  task_id uuid NOT NULL REFERENCES task_jobs(id) ON DELETE CASCADE,
  sequence integer NOT NULL,
  event_type text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE(task_id,sequence)
);

CREATE TABLE IF NOT EXISTS task_schedules (
  id uuid PRIMARY KEY,
  task_type text NOT NULL,
  cron text NOT NULL,
  payload jsonb NOT NULL,
  enabled boolean NOT NULL DEFAULT true,
  next_run_at timestamptz,
  owner_id text NOT NULL,
  idempotency_prefix text NOT NULL
);
