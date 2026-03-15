-- Track the last auto-detected routing mode for stickiness in auto mode
ALTER TABLE project_chat_sessions
  ADD COLUMN IF NOT EXISTS last_routing_mode TEXT NOT NULL DEFAULT 'chat';

-- Change default so new sessions start in auto mode
ALTER TABLE project_chat_sessions ALTER COLUMN chat_mode SET DEFAULT 'auto';
