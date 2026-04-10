-- Migration 034: Remove legacy git provider columns from projects table.
-- These were superseded by the git_provider_configs table (migration 005)
-- and the git_provider_id FK. The old columns are no longer referenced
-- anywhere in the codebase except a defensive pop in the API response.

ALTER TABLE projects DROP COLUMN IF EXISTS git_provider_type;
ALTER TABLE projects DROP COLUMN IF EXISTS git_api_base_url;
ALTER TABLE projects DROP COLUMN IF EXISTS git_token_enc;
