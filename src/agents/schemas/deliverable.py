from datetime import datetime

from pydantic import BaseModel


class DeliverableOut(BaseModel):
    id: str
    todo_id: str
    agent_run_id: str | None = None
    sub_task_id: str | None = None
    type: str
    title: str
    content_md: str | None = None
    content_json: dict | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    pr_state: str | None = None
    branch_name: str | None = None
    status: str
    reviewer_notes: str | None = None
    created_at: datetime
    updated_at: datetime
