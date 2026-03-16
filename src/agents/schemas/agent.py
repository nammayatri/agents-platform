import re
from dataclasses import dataclass, field


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


