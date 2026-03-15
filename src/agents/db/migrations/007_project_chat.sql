-- Project-level chat messages (not tied to a specific todo)
CREATE TABLE project_chat_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL DEFAULT 'user',   -- 'user' | 'assistant' | 'system'
    content         TEXT NOT NULL,
    metadata_json   JSONB,                          -- e.g. {"action": "create_task", "task_id": "..."}
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_project_chat_project ON project_chat_messages(project_id, created_at);
