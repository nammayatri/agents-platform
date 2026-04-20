-- Task pod tracking: maps todo_items to their dedicated K8s pods
CREATE TABLE IF NOT EXISTS task_pods (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    todo_id         UUID NOT NULL REFERENCES todo_items(id) ON DELETE CASCADE,
    project_id      UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    -- K8s resource identifiers
    pod_name        TEXT NOT NULL,
    pvc_name        TEXT NOT NULL,
    namespace       TEXT NOT NULL,

    -- Pod connectivity
    pod_ip          TEXT,           -- cluster-internal IP once running
    pod_port        INTEGER DEFAULT 8000,

    -- Lifecycle
    state           TEXT NOT NULL DEFAULT 'creating',
        -- creating → running → stopping → terminated
        -- creating → failed
    image           TEXT NOT NULL,
    pvc_size_gb     INTEGER NOT NULL DEFAULT 20,
    boot_script     TEXT,          -- optional startup script

    -- Observability
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    stopped_at      TIMESTAMPTZ,

    CONSTRAINT uq_task_pods_todo UNIQUE (todo_id)
);

CREATE INDEX IF NOT EXISTS idx_task_pods_state ON task_pods(state)
    WHERE state IN ('creating', 'running', 'stopping');
CREATE INDEX IF NOT EXISTS idx_task_pods_project ON task_pods(project_id);
