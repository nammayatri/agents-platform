-- Link project chat sessions to todo items (bidirectional)
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS chat_session_id UUID REFERENCES project_chat_sessions(id);
ALTER TABLE project_chat_sessions ADD COLUMN IF NOT EXISTS linked_todo_id UUID REFERENCES todo_items(id);
CREATE INDEX IF NOT EXISTS idx_todo_items_chat_session ON todo_items(chat_session_id) WHERE chat_session_id IS NOT NULL;
