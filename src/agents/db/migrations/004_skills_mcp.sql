-- Skills: reusable prompt-based capabilities that agents can use
CREATE TABLE skills (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    -- The skill prompt/instructions that get injected into agent context
    prompt          TEXT NOT NULL,
    -- Skill category for organization
    category        TEXT DEFAULT 'general',  -- 'coding' | 'testing' | 'docs' | 'devops' | 'general'
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_skills_owner ON skills(owner_id);

-- MCP Servers: Model Context Protocol server configurations
CREATE TABLE mcp_servers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    -- Connection config
    command         TEXT NOT NULL,           -- e.g. 'npx', 'uvx', 'python'
    args            TEXT[] DEFAULT '{}',     -- e.g. ['-y', '@modelcontextprotocol/server-github']
    env_json        JSONB DEFAULT '{}',     -- environment variables (sensitive values encrypted)
    -- Transport type
    transport       TEXT DEFAULT 'stdio',    -- 'stdio' | 'sse' | 'streamable-http'
    url             TEXT,                    -- for sse/http transports
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_mcp_servers_owner ON mcp_servers(owner_id);

-- Per-project skill enablement (default: all enabled)
-- Only rows in this table represent DISABLED skills for a project
CREATE TABLE project_disabled_skills (
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    skill_id        UUID NOT NULL REFERENCES skills(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, skill_id)
);

-- Per-project MCP server enablement (default: all enabled)
-- Only rows in this table represent DISABLED MCP servers for a project
CREATE TABLE project_disabled_mcp_servers (
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    mcp_server_id   UUID NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, mcp_server_id)
);

-- Per-project provider enablement (default: all enabled)
-- Only rows in this table represent DISABLED providers for a project
CREATE TABLE project_disabled_providers (
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    provider_id     UUID NOT NULL REFERENCES ai_provider_configs(id) ON DELETE CASCADE,
    PRIMARY KEY (project_id, provider_id)
);
