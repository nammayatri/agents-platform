-- Project memories: persistent learnings extracted from completed tasks
-- Allows agents to learn from past successes/failures within a project

CREATE TABLE IF NOT EXISTS project_memories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id UUID NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    category TEXT NOT NULL,          -- 'architecture', 'pattern', 'convention', 'pitfall', 'dependency'
    content TEXT NOT NULL,
    source_todo_id UUID REFERENCES todo_items(id) ON DELETE SET NULL,
    confidence FLOAT DEFAULT 1.0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_memories_project ON project_memories(project_id);
CREATE INDEX IF NOT EXISTS idx_memories_category ON project_memories(project_id, category);
