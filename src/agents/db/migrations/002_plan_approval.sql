-- Add plan_json column to store structured plan for human review
ALTER TABLE todo_items ADD COLUMN IF NOT EXISTS plan_json JSONB;
