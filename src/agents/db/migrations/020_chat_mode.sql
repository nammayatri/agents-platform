-- Add chat_mode column to project_chat_sessions
-- Replaces the binary plan_mode toggle with a multi-mode dropdown:
-- 'chat' (default), 'plan', 'debug', 'create_task'

ALTER TABLE project_chat_sessions
  ADD COLUMN IF NOT EXISTS chat_mode TEXT NOT NULL DEFAULT 'chat';

-- Backfill: sessions with plan_mode=true get chat_mode='plan'
UPDATE project_chat_sessions SET chat_mode = 'plan' WHERE plan_mode = TRUE AND chat_mode = 'chat';
