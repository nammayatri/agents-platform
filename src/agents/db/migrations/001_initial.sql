-- Initial schema for Agent Orchestration Platform

-- Users
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL DEFAULT 'user',  -- 'user' | 'admin'
    avatar_url      TEXT,
    settings_json   JSONB NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_users_email ON users(email);

-- Notification channels (Slack, Gmail, etc.) per user
CREATE TABLE notification_channels (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel_type    TEXT NOT NULL,         -- 'slack' | 'email' | 'webhook'
    display_name    TEXT NOT NULL,         -- "Work Slack", "Personal Gmail"
    config_json     JSONB NOT NULL,        -- {webhook_url, email, slack_token, channel_id, ...}
    is_active       BOOLEAN DEFAULT TRUE,
    notify_on       TEXT[] DEFAULT '{stuck,failed,completed,review}',  -- which events trigger notifications
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_notif_channels_user ON notification_channels(user_id);

-- AI provider configs
CREATE TABLE ai_provider_configs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID REFERENCES users(id) ON DELETE CASCADE,  -- NULL = system-level default
    provider_type   TEXT NOT NULL,        -- 'anthropic' | 'openai' | 'self_hosted'
    display_name    TEXT NOT NULL,
    api_base_url    TEXT,
    api_key_enc     TEXT,
    default_model   TEXT NOT NULL,
    fast_model      TEXT,
    max_tokens      INTEGER DEFAULT 4096,
    temperature     REAL DEFAULT 0.1,
    extra_config    JSONB DEFAULT '{}',
    is_active       BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_provider_owner ON ai_provider_configs(owner_id);

-- Projects
CREATE TABLE projects (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_id        UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    description     TEXT,
    repo_url        TEXT,
    default_branch  TEXT DEFAULT 'main',
    ai_provider_id  UUID REFERENCES ai_provider_configs(id),
    context_docs    JSONB DEFAULT '[]',
    settings_json   JSONB DEFAULT '{}',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_projects_owner ON projects(owner_id);

-- TODO items (central entity)
CREATE TABLE todo_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    creator_id      UUID NOT NULL REFERENCES users(id),

    -- Content
    title           TEXT NOT NULL,
    description     TEXT,
    priority        TEXT DEFAULT 'medium',  -- 'critical' | 'high' | 'medium' | 'low'
    labels          TEXT[] DEFAULT '{}',
    task_type       TEXT DEFAULT 'code',    -- 'code' | 'research' | 'document' | 'general'

    -- Intake interview results (gathered by AI before execution)
    intake_data     JSONB DEFAULT '{}',    -- structured Q&A from intake interview

    -- State machine
    state           TEXT NOT NULL DEFAULT 'intake',
    sub_state       TEXT,
    state_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- AI provider override
    ai_provider_id  UUID REFERENCES ai_provider_configs(id),

    -- Orchestration
    coordinator_session_id TEXT,
    retry_count     INTEGER DEFAULT 0,
    max_retries     INTEGER DEFAULT 3,
    error_message   TEXT,

    -- Result
    result_summary  TEXT,

    -- Metrics
    estimated_tokens INTEGER DEFAULT 0,
    actual_tokens   INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0,

    -- Notifications
    stuck_notified_at TIMESTAMPTZ,         -- last time we notified human about being stuck

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX idx_todos_project ON todo_items(project_id);
CREATE INDEX idx_todos_creator ON todo_items(creator_id);
CREATE INDEX idx_todos_state ON todo_items(state);
CREATE INDEX idx_todos_polling ON todo_items(state, updated_at)
    WHERE state IN ('intake', 'planning', 'in_progress');

-- Sub-tasks (orchestrator decomposes TODO into these)
CREATE TABLE sub_tasks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    todo_id         UUID NOT NULL REFERENCES todo_items(id) ON DELETE CASCADE,
    parent_id       UUID REFERENCES sub_tasks(id),  -- for nested sub-tasks

    title           TEXT NOT NULL,
    description     TEXT,
    agent_role      TEXT NOT NULL,         -- 'planner' | 'coder' | 'reviewer' | 'tester' | 'pr_creator' | 'report_writer'
    execution_order INTEGER DEFAULT 0,    -- ordering within siblings (0 = can run in parallel)
    depends_on      UUID[] DEFAULT '{}',  -- sub_task IDs that must complete first

    -- State
    status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'assigned' | 'running' | 'completed' | 'failed'
    assigned_agent_run_id UUID,

    -- Input/Output
    input_context   JSONB DEFAULT '{}',
    output_result   JSONB,

    -- Progress
    progress_pct    INTEGER DEFAULT 0,
    progress_message TEXT,

    -- Error tracking
    error_message   TEXT,
    retry_count     INTEGER DEFAULT 0,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX idx_subtasks_todo ON sub_tasks(todo_id);
CREATE INDEX idx_subtasks_status ON sub_tasks(status) WHERE status IN ('pending', 'assigned', 'running');

-- Agent runs
CREATE TABLE agent_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    todo_id         UUID NOT NULL REFERENCES todo_items(id) ON DELETE CASCADE,
    sub_task_id     UUID REFERENCES sub_tasks(id),

    agent_role      TEXT NOT NULL,
    agent_model     TEXT NOT NULL,
    provider_type   TEXT NOT NULL,

    status          TEXT NOT NULL DEFAULT 'running',  -- 'running' | 'completed' | 'failed' | 'cancelled'

    input_context   JSONB,
    output_result   JSONB,

    progress_pct    INTEGER DEFAULT 0,
    progress_message TEXT,

    tokens_input    INTEGER DEFAULT 0,
    tokens_output   INTEGER DEFAULT 0,
    duration_ms     INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0,

    error_type      TEXT,    -- 'transient' | 'llm_recoverable' | 'user_fixable' | 'fatal'
    error_detail    TEXT,

    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);
CREATE INDEX idx_agent_runs_todo ON agent_runs(todo_id);
CREATE INDEX idx_agent_runs_subtask ON agent_runs(sub_task_id);
CREATE INDEX idx_agent_runs_status ON agent_runs(status) WHERE status = 'running';

-- Chat messages (per-task)
CREATE TABLE chat_messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    todo_id         UUID NOT NULL REFERENCES todo_items(id) ON DELETE CASCADE,

    role            TEXT NOT NULL,         -- 'user' | 'assistant' | 'system'
    content         TEXT NOT NULL,

    agent_run_id    UUID REFERENCES agent_runs(id),
    metadata_json   JSONB DEFAULT '{}',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_chat_todo ON chat_messages(todo_id, created_at);

-- Deliverables
CREATE TABLE deliverables (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    todo_id         UUID NOT NULL REFERENCES todo_items(id) ON DELETE CASCADE,
    agent_run_id    UUID REFERENCES agent_runs(id),
    sub_task_id     UUID REFERENCES sub_tasks(id),

    type            TEXT NOT NULL,         -- 'pull_request' | 'report' | 'code_diff' | 'document' | 'test_results'
    title           TEXT NOT NULL,

    content_md      TEXT,
    content_json    JSONB,
    file_path       TEXT,

    -- PR-specific
    pr_url          TEXT,
    pr_number       INTEGER,
    pr_state        TEXT,
    branch_name     TEXT,

    status          TEXT DEFAULT 'pending',  -- 'pending' | 'approved' | 'rejected' | 'needs_revision'
    reviewer_notes  TEXT,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_deliverables_todo ON deliverables(todo_id);

-- Orchestrator locks
CREATE TABLE orchestrator_locks (
    todo_id         UUID PRIMARY KEY REFERENCES todo_items(id) ON DELETE CASCADE,
    worker_id       TEXT NOT NULL,
    locked_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    heartbeat_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL
);

-- Audit log
CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    todo_id         UUID REFERENCES todo_items(id),
    user_id         UUID REFERENCES users(id),
    agent_run_id    UUID REFERENCES agent_runs(id),

    action          TEXT NOT NULL,
    detail          TEXT NOT NULL DEFAULT '',
    metadata_json   JSONB DEFAULT '{}',

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX idx_audit_todo ON audit_log(todo_id);
CREATE INDEX idx_audit_created ON audit_log(created_at);
