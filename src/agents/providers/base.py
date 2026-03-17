"""Abstract AI provider interface.

All provider implementations must implement this interface.
This allows the system to work with any LLM provider (Anthropic, OpenAI,
self-hosted via OpenAI-compatible API) through a uniform interface.
"""

from __future__ import annotations

import json as _json
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
        tool_choice: str | dict | None = None,
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
        tool_choice: str | dict | None = None,
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

    @abstractmethod
    async def list_models(self) -> list[dict]:
        """Return available models for this provider.

        Each dict: {"id": "model-id", "name": "Display Name", "is_default": bool}
        """
        ...


# Type alias for tool executor callbacks:
#   async def executor(tool_name: str, tool_args: dict) -> str
ToolExecutor = Callable[[str, dict], Awaitable[str]]


ActivityCallback = Callable[[str], Awaitable[None]]

# Structured tool event callback for streaming execution visibility
ToolEventCallback = Callable[[dict], Awaitable[None]]

# Callback to check for injected user messages between tool rounds
InjectCheckCallback = Callable[[], Awaitable[str | None]]


async def run_tool_loop(
    provider: AIProvider,
    messages: list[LLMMessage],
    *,
    tools: list[dict] | None = None,
    tool_executor: ToolExecutor,
    max_rounds: int = 10,
    on_tool_round: Callable[[int, LLMResponse], Awaitable[None]] | None = None,
    on_activity: ActivityCallback | None = None,
    on_tool_event: ToolEventCallback | None = None,
    on_inject_check: InjectCheckCallback | None = None,
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
        on_activity: Optional callback for granular activity events (UI live log).
        on_tool_event: Optional structured event callback for streaming execution
            visibility. Receives dicts like ``{"type": "tool_start", "name": ...}``.
        on_inject_check: Optional callback to poll for user-injected messages
            between tool rounds. Called after tools execute, before the next LLM
            call.  Return a string to inject or ``None`` for no pending message.
        **send_kwargs: Forwarded to ``provider.send_message`` (temperature, model…).

    Returns:
        ``(accumulated_content, final_response)`` — the content string joins all
        intermediate text with the final response; the ``LLMResponse`` is the last
        one returned by the provider.
    """
    if tools:
        send_kwargs["tools"] = tools

    # Build known-tools set for hallucination detection
    known_tool_names = {t["name"] for t in tools} if tools else set()

    n_tools = len(tools) if tools else 0
    logger.info("tool_loop: starting (tools=%d, max_rounds=%d)", n_tools, max_rounds)

    if on_activity:
        await on_activity("Thinking...")

    response = await provider.send_message(messages, **send_kwargs)

    # After first call, drop tool_choice so subsequent rounds use auto
    send_kwargs.pop("tool_choice", None)

    logger.info(
        "tool_loop: initial response stop_reason=%s tool_calls=%d tokens_in=%d tokens_out=%d",
        response.stop_reason,
        len(response.tool_calls) if response.tool_calls else 0,
        response.tokens_input,
        response.tokens_output,
    )

    if on_activity:
        n_tc = len(response.tool_calls) if response.tool_calls else 0
        if n_tc > 0:
            tool_names = [tc["name"] for tc in response.tool_calls]
            await on_activity(f"Agent will: {', '.join(tool_names)}")
        else:
            await on_activity("Agent responded")

    # Fire initial LLM response event
    if on_tool_event:
        await on_tool_event({
            "type": "llm_thinking",
            "tokens_in": response.tokens_input,
            "tokens_out": response.tokens_output,
            "round": 0,
        })

    content_parts: list[str] = []
    tools_called: list[str] = []      # track tool names across rounds
    total_tool_calls = 0
    loop_count = 0
    nudged = False

    # Nudge: if tools were provided but the model wrote text instead of calling
    # them, inject a correction and retry once.  This catches models (e.g.
    # kimi-latest) that describe what they *would* do instead of acting.
    if tools and not response.tool_calls and response.content:
        logger.info("tool_loop: model produced text without tool calls, nudging")
        if on_activity:
            await on_activity("Nudging agent to use tools...")
        messages.append(LLMMessage(
            role="assistant", content=response.content,
        ))
        tool_names_hint = ", ".join(sorted({t["name"] for t in tools}))
        messages.append(LLMMessage(
            role="user",
            content=(
                "You have tools available and MUST use them to answer. "
                "Do NOT describe what you would do — actually call the tools now. "
                f"Available tools: {tool_names_hint}"
            ),
        ))
        if response.content:
            content_parts.append(response.content)
        response = await provider.send_message(messages, **send_kwargs)
        nudged = True
        logger.info(
            "tool_loop: nudge response stop_reason=%s tool_calls=%d",
            response.stop_reason,
            len(response.tool_calls) if response.tool_calls else 0,
        )
        if on_tool_event:
            await on_tool_event({
                "type": "llm_thinking",
                "tokens_in": response.tokens_input,
                "tokens_out": response.tokens_output,
                "round": 0,
                "nudged": True,
            })

    while response.tool_calls and loop_count < max_rounds:
        loop_count += 1

        tool_names = [tc["name"] for tc in response.tool_calls]
        tools_called.extend(tool_names)
        total_tool_calls += len(tool_names)
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
        total_tools = len(response.tool_calls)
        for tool_idx, tc in enumerate(response.tool_calls):
            tc_name = tc["name"]
            tc_args = tc.get("arguments", {})
            logger.info("tool_loop: round %d — exec %s args_keys=%s", loop_count, tc_name, list(tc_args.keys()))

            # Validate tool name against known tools
            if known_tool_names and tc_name not in known_tool_names:
                logger.warning(
                    "tool_loop: round %d — hallucinated tool '%s', known: %s",
                    loop_count, tc_name, sorted(known_tool_names),
                )
                result_text = _json.dumps({
                    "error": f"Tool '{tc_name}' does not exist. "
                    f"Available tools: {', '.join(sorted(known_tool_names))}",
                })
                tool_results.append({"tool_use_id": tc["id"], "content": result_text})
                if on_tool_event:
                    await on_tool_event({
                        "type": "tool_result",
                        "name": tc_name,
                        "result_preview": result_text[:200],
                        "chars": len(result_text),
                        "error": True,
                    })
                continue

            # Report tool execution activity
            if on_activity:
                tool_detail = _tool_activity_summary(tc_name, tc_args)
                await on_activity(tool_detail)

            # Fire structured tool_start event
            if on_tool_event:
                await on_tool_event({
                    "type": "tool_start",
                    "name": tc_name,
                    "args_summary": _tool_activity_summary(tc_name, tc_args),
                    "tool_index": tool_idx + 1,
                    "total_tools": total_tools,
                })

            result_text = await tool_executor(tc_name, tc_args)
            result_preview = (result_text[:200] + "...") if len(result_text) > 200 else result_text
            logger.debug("tool_loop: round %d — %s result (%d chars): %s", loop_count, tc_name, len(result_text), result_preview)
            tool_results.append({"tool_use_id": tc["id"], "content": result_text})

            # Fire structured tool_result event
            if on_tool_event:
                await on_tool_event({
                    "type": "tool_result",
                    "name": tc_name,
                    "result_preview": result_preview,
                    "chars": len(result_text),
                })

        messages.append(LLMMessage(role="user", content="", tool_results=tool_results))

        # Check for injected user messages between tool rounds
        if on_inject_check:
            injected_parts: list[str] = []
            while True:
                injected = await on_inject_check()
                if injected is None:
                    break
                injected_parts.append(injected)
            if injected_parts:
                combined = "\n\n".join(injected_parts)
                messages.append(LLMMessage(
                    role="user",
                    content=f"[USER GUIDANCE]: {combined}",
                ))
                if on_activity:
                    preview = combined[:80] + ("..." if len(combined) > 80 else "")
                    await on_activity(f"User guidance received: {preview}")
                logger.info("tool_loop: injected %d user message(s) before round %d",
                            len(injected_parts), loop_count + 1)

        if on_activity:
            await on_activity(f"Thinking... (round {loop_count + 1})")

        response = await provider.send_message(messages, **send_kwargs)

        logger.info(
            "tool_loop: round %d response stop_reason=%s tool_calls=%d",
            loop_count, response.stop_reason,
            len(response.tool_calls) if response.tool_calls else 0,
        )

        if on_activity:
            n_tc = len(response.tool_calls) if response.tool_calls else 0
            if n_tc > 0:
                tool_names = [tc["name"] for tc in response.tool_calls]
                await on_activity(f"Agent will: {', '.join(tool_names)}")
            else:
                await on_activity(f"Agent responded (round {loop_count})")

        # Fire LLM response event
        if on_tool_event:
            await on_tool_event({
                "type": "llm_thinking",
                "tokens_in": response.tokens_input,
                "tokens_out": response.tokens_output,
                "round": loop_count,
            })

    truncated = loop_count >= max_rounds and bool(response.tool_calls)
    if truncated:
        logger.warning("tool_loop: hit max_rounds=%d, stopping with pending tool calls", max_rounds)
        response.stop_reason = "max_tool_rounds"

    logger.info("tool_loop: finished after %d rounds, final content_len=%d", loop_count, len(response.content or ""))

    if on_activity:
        await on_activity(f"Done ({loop_count} tool rounds)")

    # Combine pre-tool text with the final response
    if content_parts:
        content_parts.append(response.content or "")
        content = "\n\n".join(p for p in content_parts if p)
    else:
        content = response.content

    # If the loop hit the round limit, append a visible note so the user
    # knows the agent was cut off mid-exploration and can say "continue".
    if truncated:
        content = (content or "") + (
            f"\n\n---\n*Agent reached the tool round limit ({max_rounds} rounds, "
            f"{total_tool_calls} tool calls). Say **continue** to keep going.*"
        )

    # Attach execution summary to the response for callers that want visibility
    # into what happened during the tool loop.
    unique_tools = list(dict.fromkeys(tools_called))  # preserves order, dedupes
    response.tool_summary = {
        "rounds": loop_count,
        "tools_called": unique_tools,
        "total_tool_calls": total_tool_calls,
        "nudged": nudged,
    }

    return content, response


def _tool_activity_summary(name: str, args: dict) -> str:
    """One-line human-readable summary of a tool call for the activity log."""
    import os.path as _osp

    if name == "write_file":
        path = args.get("path", "?")
        size = len(args.get("content", ""))
        return f"Writing {_osp.basename(path)} ({size:,} chars)"
    if name == "read_file":
        return f"Reading {_osp.basename(args.get('path', '?'))}"
    if name == "list_directory":
        path = args.get("path", "?")
        return f"Listing {_osp.basename(path)}/"
    if name == "search_files":
        pattern = args.get("pattern", "?")
        glob = args.get("file_glob", "*")
        return f"Searching '{pattern}' in {glob}"
    if name == "run_command":
        cmd = args.get("command", "?")
        if len(cmd) > 120:
            cmd = cmd[:117] + "..."
        return f"Running: {cmd}"
    if name == "semantic_search":
        query = args.get("query", "?")[:60]
        return f'Searching code: "{query}"'
    if name == "edit_file":
        path = args.get("path", "?")
        old_len = len(args.get("old_text", ""))
        new_len = len(args.get("new_text", ""))
        return f"Editing {_osp.basename(path)} ({old_len}\u2192{new_len} chars)"
    # Generic fallback
    return f"Tool: {name}"
