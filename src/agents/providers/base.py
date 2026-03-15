"""Abstract AI provider interface.

All provider implementations must implement this interface.
This allows the system to work with any LLM provider (Anthropic, OpenAI,
self-hosted via OpenAI-compatible API) through a uniform interface.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Awaitable
from typing import Any

from agents.schemas.agent import LLMMessage, LLMResponse, StreamChunk

logger = logging.getLogger(__name__)


class AIProvider(ABC):
    """Abstract interface for AI providers."""

    provider_type: str  # 'anthropic' | 'openai' | 'self_hosted'
    default_model: str
    fast_model: str | None

    @abstractmethod
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
        """Send messages and get a complete response."""
        ...

    @abstractmethod
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
        """Stream a response. Used for chat UI."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Test provider connectivity."""
        ...

    async def health_check_detailed(self) -> bool:
        """Test provider connectivity. Raises on failure with error details."""
        # Default implementation calls health_check but lets exceptions propagate.
        # Subclasses can override for richer error reporting.
        return await self.health_check()

    @abstractmethod
    def estimate_cost(self, tokens_input: int, tokens_output: int, model: str) -> float:
        """Estimate cost in USD for given token counts."""
        ...

    def get_model(self, use_fast: bool = False) -> str:
        if use_fast and self.fast_model:
            return self.fast_model
        return self.default_model


# Type alias for tool executor callbacks:
#   async def executor(tool_name: str, tool_args: dict) -> str
ToolExecutor = Callable[[str, dict], Awaitable[str]]


async def run_tool_loop(
    provider: AIProvider,
    messages: list[LLMMessage],
    *,
    tools: list[dict] | None = None,
    tool_executor: ToolExecutor,
    max_rounds: int = 10,
    on_tool_round: Callable[[int, LLMResponse], Awaitable[None]] | None = None,
    **send_kwargs: Any,
) -> tuple[str, LLMResponse]:
    """Standard tool-call loop with content accumulation.

    Sends an initial message, then iterates on tool calls up to *max_rounds*.
    Text content produced before each tool call is accumulated so nothing is lost.

    Args:
        provider: The AI provider to call.
        messages: Conversation so far (mutated in-place with tool-round messages).
        tools: Tool definitions forwarded to the provider.
        tool_executor: ``async (name, arguments) -> result_text`` callback.
        max_rounds: Hard cap on tool-call iterations.
        on_tool_round: Optional ``async (round, response)`` hook (e.g. progress).
        **send_kwargs: Forwarded to ``provider.send_message`` (temperature, model…).

    Returns:
        ``(accumulated_content, final_response)`` — the content string joins all
        intermediate text with the final response; the ``LLMResponse`` is the last
        one returned by the provider.
    """
    if tools:
        send_kwargs["tools"] = tools

    n_tools = len(tools) if tools else 0
    logger.info("tool_loop: starting (tools=%d, max_rounds=%d)", n_tools, max_rounds)

    response = await provider.send_message(messages, **send_kwargs)

    logger.info(
        "tool_loop: initial response stop_reason=%s tool_calls=%d tokens_in=%d tokens_out=%d",
        response.stop_reason,
        len(response.tool_calls) if response.tool_calls else 0,
        response.tokens_input,
        response.tokens_output,
    )

    content_parts: list[str] = []
    loop_count = 0

    while response.stop_reason == "tool_use" and response.tool_calls and loop_count < max_rounds:
        loop_count += 1

        tool_names = [tc["name"] for tc in response.tool_calls]
        logger.info("tool_loop: round %d/%d — calling tools: %s", loop_count, max_rounds, tool_names)

        if on_tool_round:
            await on_tool_round(loop_count, response)

        # Preserve any text the model produced before calling tools
        if response.content:
            content_parts.append(response.content)

        messages.append(LLMMessage(
            role="assistant",
            content=response.content,
            tool_calls=response.tool_calls,
        ))

        tool_results = []
        for tc in response.tool_calls:
            tc_name = tc["name"]
            tc_args = tc.get("arguments", {})
            logger.info("tool_loop: round %d — exec %s args_keys=%s", loop_count, tc_name, list(tc_args.keys()))
            result_text = await tool_executor(tc_name, tc_args)
            result_preview = (result_text[:200] + "...") if len(result_text) > 200 else result_text
            logger.debug("tool_loop: round %d — %s result (%d chars): %s", loop_count, tc_name, len(result_text), result_preview)
            tool_results.append({"tool_use_id": tc["id"], "content": result_text})

        messages.append(LLMMessage(role="user", content="", tool_results=tool_results))
        response = await provider.send_message(messages, **send_kwargs)

        logger.info(
            "tool_loop: round %d response stop_reason=%s tool_calls=%d",
            loop_count, response.stop_reason,
            len(response.tool_calls) if response.tool_calls else 0,
        )

    if loop_count >= max_rounds and response.stop_reason == "tool_use":
        logger.warning("tool_loop: hit max_rounds=%d, stopping with pending tool calls", max_rounds)

    logger.info("tool_loop: finished after %d rounds, final content_len=%d", loop_count, len(response.content or ""))

    # Combine pre-tool text with the final response
    if content_parts:
        content_parts.append(response.content or "")
        content = "\n\n".join(p for p in content_parts if p)
    else:
        content = response.content

    return content, response
