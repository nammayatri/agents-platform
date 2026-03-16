from collections.abc import AsyncIterator

import anthropic

from agents.providers.base import AIProvider
from agents.schemas.agent import LLMMessage, LLMResponse, StreamChunk

# Pricing per 1M tokens
PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    "claude-haiku-3-20250311": {"input": 0.25, "output": 1.25},
}


class AnthropicProvider(AIProvider):
    provider_type = "anthropic"

    def __init__(
        self,
        api_key: str,
        default_model: str = "claude-sonnet-4-20250514",
        fast_model: str | None = "claude-haiku-3-20250311",
    ):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.default_model = default_model
        self.fast_model = fast_model

    def _build_messages(
        self, messages: list[LLMMessage]
    ) -> tuple[str | None, list[dict]]:
        """Separate system prompt from messages for Anthropic API."""
        system_prompt = None
        api_messages = []

        for msg in messages:
            if msg.role == "system":
                system_prompt = msg.content
            elif msg.role == "assistant" and msg.tool_calls:
                # Assistant message with tool use blocks
                content_blocks = []
                if msg.content:
                    content_blocks.append({"type": "text", "text": msg.content})
                for tc in msg.tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": tc["name"],
                        "input": tc.get("arguments", {}),
                    })
                api_messages.append({"role": "assistant", "content": content_blocks})
            elif msg.tool_results:
                # User message with tool results
                content_blocks = []
                for tr in msg.tool_results:
                    content_blocks.append({
                        "type": "tool_result",
                        "tool_use_id": tr["tool_use_id"],
                        "content": tr["content"],
                    })
                api_messages.append({"role": "user", "content": content_blocks})
            else:
                api_messages.append({"role": msg.role, "content": msg.content})

        return system_prompt, api_messages

    def _build_tools(self, tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        # Anthropic tool format
        return [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters", t.get("input_schema", {})),
            }
            for t in tools
        ]

    async def send_message(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        sys_from_messages, api_messages = self._build_messages(messages)
        system = system_prompt or sys_from_messages

        kwargs: dict = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system
        api_tools = self._build_tools(tools)
        if api_tools:
            kwargs["tools"] = api_tools

        response = await self.client.messages.create(**kwargs)

        # Extract text content
        content = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "arguments": block.input,
                })

        cost = self.estimate_cost(
            response.usage.input_tokens,
            response.usage.output_tokens,
            kwargs["model"],
        )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=response.usage.input_tokens,
            tokens_output=response.usage.output_tokens,
            model=kwargs["model"],
            stop_reason=response.stop_reason or "",
            cost_usd=cost,
            cached_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        )

    async def stream_message(
        self,
        messages: list[LLMMessage],
        *,
        model: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        system_prompt: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        sys_from_messages, api_messages = self._build_messages(messages)
        system = system_prompt or sys_from_messages

        kwargs: dict = {
            "model": model or self.default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        if system:
            kwargs["system"] = system

        async with self.client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield StreamChunk(delta=text, chunk_type="text")

    async def health_check(self) -> bool:
        try:
            return await self.health_check_detailed()
        except Exception:
            return False

    async def health_check_detailed(self) -> bool:
        response = await self.client.messages.create(
            model=self.fast_model or self.default_model,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        return bool(response.content)

    def estimate_cost(self, tokens_input: int, tokens_output: int, model: str) -> float:
        pricing = PRICING.get(model, {"input": 3.0, "output": 15.0})
        return (tokens_input * pricing["input"] + tokens_output * pricing["output"]) / 1_000_000

    async def list_models(self) -> list[dict]:
        known = [
            {"id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
            {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
            {"id": "claude-haiku-3-20250311", "name": "Claude Haiku 3"},
        ]
        return [
            {**m, "is_default": m["id"] == self.default_model}
            for m in known
        ]
