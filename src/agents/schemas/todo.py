from datetime import datetime

from pydantic import BaseModel


class CreateTodoInput(BaseModel):
    title: str
    description: str | None = None
    priority: str = "medium"
    labels: list[str] = []
    task_type: str = "code"  # 'code' | 'research' | 'document' | 'general'
    ai_provider_id: str | None = None
    scheduled_at: str | None = None  # ISO 8601 datetime for deferred execution
    rules_override_json: dict | None = None  # Per-task work rule overrides
    max_iterations: int | None = None  # RALPH loop max iterations (default 50)


class UpdateTodoInput(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    labels: list[str] | None = None


class SubTaskOut(BaseModel):
    id: str
    todo_id: str
    parent_id: str | None = None
    title: str
    description: str | None = None
    agent_role: str
    execution_order: int
    status: str
    progress_pct: int
    progress_message: str | None = None
    error_message: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class TodoOut(BaseModel):
    id: str
    project_id: str
    creator_id: str
    title: str
    description: str | None = None
    priority: str
    labels: list[str]
    task_type: str
    intake_data: dict | None = None
    state: str
    sub_state: str | None = None
    state_changed_at: datetime
    retry_count: int
    error_message: str | None = None
    result_summary: str | None = None
    actual_tokens: int
    cost_usd: float
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    sub_tasks: list[SubTaskOut] = []


class TodoListOut(BaseModel):
    id: str
    title: str
    priority: str
    state: str
    sub_state: str | None = None
    task_type: str
    labels: list[str]
    created_at: datetime
    updated_at: datetime


class RejectInput(BaseModel):
    feedback: str


class RequestChangesInput(BaseModel):
    feedback: str


class RejectPlanInput(BaseModel):
    feedback: str
