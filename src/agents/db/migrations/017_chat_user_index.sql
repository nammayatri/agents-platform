-- Index on user_id for project_chat_messages (queried in session listing and message fetching)
CREATE INDEX IF NOT EXISTS idx_project_chat_user ON project_chat_messages(user_id);
