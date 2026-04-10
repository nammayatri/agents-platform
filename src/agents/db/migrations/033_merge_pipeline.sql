-- Migration 033: Merge pipeline — standalone pipeline runs triggered by PR merges
-- Independent of the task/todo system. Tracks test verification and deploy phases.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    -- Which repo this run is for ("main" or dependency name)
    repo_name       TEXT NOT NULL DEFAULT 'main',

    -- PR metadata from webhook payload
    pr_number       INTEGER NOT NULL,
    pr_title        TEXT,
    branch_name     TEXT,
    commit_hash     TEXT,
    repo_url        TEXT,
    trigger_payload JSONB,          -- raw webhook payload (trimmed) for audit

    -- Pipeline status (state machine)
    status          TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'testing', 'test_passed', 'test_failed',
                          'deploying', 'deploy_success', 'deploy_failed',
                          'skipped', 'cancelled')),

    -- Test phase
    test_mode       TEXT CHECK (test_mode IN ('webhook', 'poll')),
    test_config     JSONB,          -- snapshot of config at trigger time
    test_result     JSONB,          -- { passed: bool, output: "...", received_at: "..." }
    webhook_token   TEXT,           -- unique token for webhook callback (webhook mode only)

    -- Deploy phase
    deploy_config   JSONB,          -- snapshot of deploy config at trigger time
    deploy_result   JSONB,          -- { status_code, response_body, ... }

    -- Timing
    started_at      TIMESTAMPTZ,
    test_completed_at   TIMESTAMPTZ,
    deploy_completed_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_project
    ON pipeline_runs(project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_active
    ON pipeline_runs(status)
    WHERE status IN ('pending', 'testing', 'deploying');

CREATE UNIQUE INDEX IF NOT EXISTS idx_pipeline_runs_webhook_token
    ON pipeline_runs(webhook_token)
    WHERE webhook_token IS NOT NULL;
