-- Store resolved workspace path on each subtask.
-- This is the single source of truth for where the subtask's work lives on disk,
-- replacing in-memory re-computation that was lost on crash/restart.
ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS workspace_path TEXT;
