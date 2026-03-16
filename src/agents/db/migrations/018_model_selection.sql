-- Model selection: allow task-level and chat-session-level model overrides
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS ai_model TEXT;
ALTER TABLE project_chat_sessions ADD COLUMN IF NOT EXISTS ai_model TEXT;
