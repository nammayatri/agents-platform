-- Make plan_mode default to TRUE for new chat sessions
ALTER TABLE project_chat_sessions ALTER COLUMN plan_mode SET DEFAULT TRUE;
