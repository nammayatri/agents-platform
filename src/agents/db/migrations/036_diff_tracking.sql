-- Migration 036: Track base commit for task-level diffs and per-subtask commits.
-- base_commit: the commit hash where the task branch was created from main.
-- commit_hash on sub_tasks: the commit hash after the subtask's incremental commit.
-- Together they enable: per-subtask diff (commit_hash~1..commit_hash)
-- and overall task diff (base_commit..HEAD).

ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS base_commit TEXT;
ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS commit_hash TEXT;
