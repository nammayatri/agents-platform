-- Chat sessions for multi-chat and plan mode support
CREATE TABLE IF NOT EXISTS project_chat_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title           TEXT NOT NULL DEFAULT 'New Chat',
    mode            TEXT NOT NULL DEFAULT 'chat',   -- 'chat' | 'plan'
    plan_json       JSONB,                          -- For plan mode: stores the evolving plan
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chat_sessions_project ON project_chat_sessions(project_id, user_id);

-- Add session_id to existing messages table
ALTER TABLE project_chat_messages
    ADD COLUMN IF NOT EXISTS session_id UUID REFERENCES project_chat_sessions(id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON project_chat_messages(session_id);
