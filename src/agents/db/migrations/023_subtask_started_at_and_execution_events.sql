-- Migration 023: Add started_at timestamp and execution_events to sub_tasks
-- started_at tracks when a subtask transitions to "running"
-- execution_events stores the full tool event log for post-completion review

ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ;
ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS execution_events JSONB DEFAULT '[]';
