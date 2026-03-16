-- Architect/Editor model split configuration
-- When enabled, tasks use a powerful "architect" model for reasoning/planning
-- and a fast "editor" model for applying code changes

ALTER TABLE projects ADD COLUMN IF NOT EXISTS architect_editor_enabled BOOLEAN DEFAULT false;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS architect_model TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS editor_model TEXT;
