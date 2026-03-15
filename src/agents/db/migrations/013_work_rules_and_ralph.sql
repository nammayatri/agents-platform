-- Work rules & RALPH-style iteration support

-- Task-level overrides for project work rules
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS rules_override_json JSONB;

-- Append-only progress log (RALPH's progress.txt equivalent)
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS progress_log JSONB DEFAULT '[]';

-- Max iterations for RALPH loop (default 50)
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS max_iterations INTEGER DEFAULT 50;

-- Per-sub-task iteration log (fresh context per iteration, learnings carry forward)
ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS iteration_log JSONB DEFAULT '[]';
