import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel


def sanitize_llm_content(text: str) -> str:
    """Strip internal model markup that some providers leak into text output.

    Handles <think>, <search>, <query> blocks from models like DeepSeek,
    and orphaned closing tags from split responses.
    """
    if not text:
        return text
    # Remove complete <think>...</think> blocks
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Remove orphaned think tags
    text = re.sub(r"</?think>", "", text)
    # Remove <search>...</search> and <query>...</query> blocks
    text = re.sub(r"<search>.*?</search>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?search>", "", text)
    text = re.sub(r"<query>.*?</query>", "", text, flags=re.DOTALL)
    text = re.sub(r"</?query>", "", text)
    return text.strip()


@dataclass
class LLMMessage:
    role: str  # 'system' | 'user' | 'assistant'
    content: str
    tool_calls: list[dict] | None = None
    tool_results: list[dict] | None = None


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    tokens_input: int = 0
    tokens_output: int = 0
    model: str = ""
    stop_reason: str = ""  # 'end_turn' | 'tool_use' | 'max_tokens'
    cost_usd: float = 0.0
    cached_tokens: int = 0

    def __post_init__(self):
        if self.content:
            self.content = sanitize_llm_content(self.content)


@dataclass
class StreamChunk:
    delta: str
    chunk_type: str  # 'text' | 'tool_call_start' | 'tool_call_delta' | 'tool_call_end'
    tool_call: dict | None = None


@dataclass
class AgentResult:
    success: bool
    output: dict[str, Any] = field(default_factory=dict)
    deliverables: list[dict] = field(default_factory=list)
    error: str | None = None
    tokens_used: int = 0
    cost_usd: float = 0.0


@dataclass
class PlanStep:
    agent_role: str
    task_description: str
    context: dict = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)  # indexes of steps this depends on


@dataclass
class ExecutionPlan:
    summary: str
    steps: list[PlanStep] = field(default_factory=list)
    estimated_tokens: int = 0


class AgentRunOut(BaseModel):
    id: str
    todo_id: str
    sub_task_id: str | None = None
    agent_role: str
    agent_model: str
    provider_type: str
    status: str
    progress_pct: int
    progress_message: str | None = None
    tokens_input: int
    tokens_output: int
    duration_ms: int
    cost_usd: float
    error_type: str | None = None
    error_detail: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
