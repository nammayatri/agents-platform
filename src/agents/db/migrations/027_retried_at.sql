-- Migration 027: Add retried_at timestamp to todo_items
-- Used by the frontend to distinguish previous-run subtasks from current-run ones.
-- state_changed_at updates on every state transition, but retried_at only
-- gets set when a task is explicitly retried, giving a proper boundary.
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS retried_at TIMESTAMPTZ;
