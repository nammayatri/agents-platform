-- Review loop tracking on sub-tasks
ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS review_loop BOOLEAN DEFAULT FALSE;
ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS review_chain_id UUID REFERENCES sub_tasks(id);
ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS review_verdict TEXT;
ALTER TABLE sub_tasks ADD COLUMN IF NOT EXISTS target_repo JSONB;

-- Merge tracking on deliverables
ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ;
ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS merge_method TEXT;
ALTER TABLE deliverables ADD COLUMN IF NOT EXISTS target_repo_name TEXT;
