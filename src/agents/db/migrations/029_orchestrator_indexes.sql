-- Indexes for orchestrator efficiency

-- 1. Index on orchestrator_locks.expires_at for fallback poll LEFT JOIN
CREATE INDEX IF NOT EXISTS idx_orchestrator_locks_expires
    ON orchestrator_locks(expires_at);

-- 2. Partial index on todo_items(state, sub_state) for fallback poll filtering
CREATE INDEX IF NOT EXISTS idx_todo_items_state_substate
    ON todo_items(state, sub_state)
    WHERE state IN ('intake', 'planning', 'in_progress', 'testing');

-- 3. Composite index on sub_tasks(todo_id, status) for frequent dependency queries
CREATE INDEX IF NOT EXISTS idx_sub_tasks_todo_status
    ON sub_tasks(todo_id, status);
