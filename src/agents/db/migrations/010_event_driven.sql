-- Event-driven architecture: scheduling + plan mode toggle

-- 1. Add scheduled_at to todo_items for task scheduling
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS scheduled_at TIMESTAMPTZ;

-- Index for scheduler queries: find due scheduled tasks efficiently
CREATE INDEX IF NOT EXISTS idx_todos_scheduled
    ON todo_items(scheduled_at)
    WHERE state = 'scheduled' AND scheduled_at IS NOT NULL;

-- 2. Update the polling index to include 'scheduled' state
DROP INDEX IF EXISTS idx_todos_polling;
CREATE INDEX idx_todos_polling ON todo_items(state, updated_at)
    WHERE state IN ('intake', 'planning', 'in_progress', 'scheduled');

-- 3. Add plan_mode flag to project_chat_sessions for toggle behavior
ALTER TABLE project_chat_sessions
    ADD COLUMN IF NOT EXISTS plan_mode BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill: existing 'plan' sessions get plan_mode=TRUE
UPDATE project_chat_sessions SET plan_mode = TRUE WHERE mode = 'plan';
