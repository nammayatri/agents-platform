-- Add git provider configuration to projects
ALTER TABLE projects ADD COLUMN IF NOT EXISTS git_provider_type TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS git_api_base_url TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS git_token_enc TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS workspace_path TEXT;
