-- Git provider configs (like ai_provider_configs, but for git hosts)
CREATE TABLE git_provider_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider_type   TEXT NOT NULL,           -- 'github' | 'gitlab' | 'bitbucket' | 'custom'
    display_name    TEXT NOT NULL,
    api_base_url    TEXT,                    -- NULL = use provider default
    token_enc       TEXT,                    -- Fernet-encrypted access token
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_git_provider_owner ON git_provider_configs(owner_id);

-- Add git_provider_id FK to projects (replaces inline git_provider_type/git_api_base_url/git_token_enc)
ALTER TABLE projects ADD COLUMN IF NOT EXISTS git_provider_id UUID REFERENCES git_provider_configs(id);
