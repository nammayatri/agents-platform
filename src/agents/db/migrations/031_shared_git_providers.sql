-- Make git provider configs shareable: admin-created providers have owner_id=NULL
-- and are visible to all users (same pattern as ai_provider_configs).
ALTER TABLE git_provider_configs ALTER COLUMN owner_id DROP NOT NULL;
