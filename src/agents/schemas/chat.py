from datetime import datetime

from pydantic import BaseModel


class ChatMessageInput(BaseModel):
    content: str


class ChatMessageOut(BaseModel):
    id: str
    todo_id: str
    role: str
    content: str
    agent_run_id: str | None = None
    metadata_json: dict = {}
    created_at: datetime
