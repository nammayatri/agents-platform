-- Custom agent configurations
CREATE TABLE IF NOT EXISTS agent_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL,          -- unique role identifier (e.g. 'frontend_dev')
    description     TEXT,
    system_prompt   TEXT NOT NULL,          -- the agent's system prompt / instructions
    model_preference TEXT,                  -- optional model override (e.g. 'claude-sonnet-4-20250514')
    tools_enabled   TEXT[] DEFAULT '{}',    -- list of tool/skill names this agent can use
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_configs_owner ON agent_configs(owner_id);

-- Agent chat messages (for the agent-builder chat interface)
CREATE TABLE IF NOT EXISTS agent_chat_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,           -- 'user' | 'assistant' | 'system'
    content         TEXT NOT NULL,
    metadata_json   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_chat_user ON agent_chat_messages(user_id);
