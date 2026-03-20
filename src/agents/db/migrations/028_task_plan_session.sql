-- Store create_task mode plan in the session (deferred creation until user approves)
ALTER TABLE project_chat_sessions
  ADD COLUMN IF NOT EXISTS task_plan_json JSONB;
