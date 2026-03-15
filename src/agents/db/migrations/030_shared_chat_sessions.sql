-- Shared chat sessions: all project members can see and participate in all sessions.
-- user_id on sessions = creator, user_id on messages = sender. No column changes needed.

-- Index for project-wide session listing (previously filtered by user_id).
CREATE INDEX IF NOT EXISTS idx_chat_sessions_project_only
    ON project_chat_sessions(project_id, updated_at DESC);
