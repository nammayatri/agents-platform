-- Migration 026: Webhook secrets for inbound webhook verification
CREATE TABLE IF NOT EXISTS webhook_secrets (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id    UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider_type TEXT NOT NULL,  -- 'github' | 'gitlab'
    secret        TEXT NOT NULL,
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, provider_type)
);

CREATE INDEX IF NOT EXISTS idx_webhook_secrets_project
    ON webhook_secrets(project_id);
