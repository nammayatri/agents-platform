"""Abstract AI provider interface.

All provider implementations must implement this interface.
This allows the system to work with any LLM provider (Anthropic, OpenAI,
self-hosted via OpenAI-compatible API) through a uniform interface.
"""

from __future__ import annotations

import hashlib as _hashlib
import json as _json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Awaitable
from typing import Any

from agents.schemas.agent import LLMMessage, LLMResponse, StreamChunk
from agents.utils.token_counter import get_context_window

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

    async def send_message_streaming(
        self,
        messages: list[LLMMessage],
        *,
        on_token: Callable[[str], Awaitable[None]],
        model: str | None = None,
        tools: list[dict] | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.1,
        system_prompt: str | None = None,
        tool_choice: str | dict | None = None,
    ) -> LLMResponse:
        """Like send_message but streams text deltas via on_token callback.

        Returns the full LLMResponse (same as send_message) after streaming
        completes. Subclasses override for real streaming; the default falls
        back to send_message (no streaming).
        """
        return await self.send_message(
            messages,
            model=model,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
            tool_choice=tool_choice,
        )


# Type alias for tool executor callbacks:
#   async def executor(tool_name: str, tool_args: dict) -> str
ToolExecutor = Callable[[str, dict], Awaitable[str]]


ActivityCallback = Callable[[str], Awaitable[None]]

# Callback for streaming text deltas during LLM generation
TokenCallback = Callable[[str], Awaitable[None]]

# Structured tool event callback for streaming execution visibility
ToolEventCallback = Callable[[dict], Awaitable[None]]

# Callback to check for injected user messages between tool rounds
InjectCheckCallback = Callable[[], Awaitable[str | None]]


def _compact_messages_for_overflow(
    messages: list[LLMMessage],
    keep_recent: int = 6,
) -> list[LLMMessage]:
    """Replace old tool-round messages with a summary when approaching context limits.

    Keeps the initial context (system + first user messages) and the last
    ``keep_recent`` messages intact.  Everything in between is summarized
    into a single user message listing tool names and brief result previews.
    """
    # Identify initial context: everything up to the first non-tool user message
    head: list[LLMMessage] = []
    for m in messages:
        head.append(m)
        if m.role == "user" and not m.tool_results:
            break
    if len(messages) <= len(head) + keep_recent:
        return messages  # nothing to compact

    tail = messages[-keep_recent:]
    middle = messages[len(head):-keep_recent]

    summary_parts: list[str] = []
    for m in middle:
        if m.tool_calls:
            tools = [tc["name"] for tc in m.tool_calls]
            summary_parts.append(f"Called: {', '.join(tools)}")
        elif m.tool_results:
            for tr in m.tool_results:
                preview = (tr.get("content", "") or "")[:150]
                summary_parts.append(f"Result: {preview}")
        elif m.content and m.role == "assistant":
            summary_parts.append(f"Response: {m.content[:150]}")

    # Keep the last 20 summary entries to stay reasonably compact
    summary = "Previous exploration summary (compacted):\n" + "\n".join(summary_parts[-20:])
    return [*head, LLMMessage(role="user", content=summary), *tail]


async def _retry_on_rate_limit(
    call: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    on_activity: ActivityCallback | None = None,
) -> Any:
    """Retry an async call on rate-limit (429) or overloaded (529) errors.

    Respects ``retry-after`` headers when available, otherwise uses
    exponential backoff (5s, 10s, 20s… capped at 60s).
    """
    import asyncio as _asyncio

    for attempt in range(max_retries + 1):
        try:
            return await call()
        except Exception as e:
            status = getattr(e, "status_code", None) or getattr(e, "status", None)
            err_s = str(e).lower()
            retriable = (
                status in (429, 529)
                or ("rate" in err_s and "limit" in err_s)
                or "overloaded" in err_s
            )
            if retriable and attempt < max_retries:
                retry_after = getattr(e, "retry_after", None)
                if retry_after is None:
                    hdrs = getattr(e, "headers", {}) or {}
                    retry_after = hdrs.get("retry-after")
                wait = float(retry_after) if retry_after else min(2 ** attempt * 5, 60)
                logger.warning(
                    "Rate limit hit (attempt %d/%d), waiting %.1fs",
                    attempt + 1, max_retries, wait,
                )
                if on_activity:
                    await on_activity(f"Rate limited, retrying in {wait:.0f}s...")
                await _asyncio.sleep(wait)
                continue
            raise


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
    on_cancel_check: Callable[[], Awaitable[bool]] | None = None,
    on_token: TokenCallback | None = None,
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
        on_cancel_check: Optional ``async () -> bool`` callback polled between
            tool rounds.  Returns ``True`` to abort.  When triggered the loop
            exits with ``stop_reason="cancelled"`` on the response.
        on_token: Optional callback for streaming text deltas during LLM
            generation. When provided, uses ``provider.send_message_streaming``
            to stream tokens in real-time.
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

    # Helper: call LLM with optional token streaming
    async def _call_llm() -> LLMResponse:
        if on_token:
            return await provider.send_message_streaming(
                messages, on_token=on_token, **send_kwargs,
            )
        return await provider.send_message(messages, **send_kwargs)

    if on_activity:
        await on_activity("Thinking...")

    response = await _retry_on_rate_limit(_call_llm, on_activity=on_activity)

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
            briefs = [_tool_brief(tc["name"], tc.get("arguments", {})) for tc in response.tool_calls]
            await on_activity(f"Agent will: {', '.join(briefs)}")
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

    # Doom-loop detection: track recent (tool_name, args_hash) signatures
    _recent_sigs: list[list[str]] = []
    _DOOM_WINDOW = 5
    _DOOM_THRESHOLD = 3

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
        response = await _retry_on_rate_limit(_call_llm, on_activity=on_activity)
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
                start_event: dict = {
                    "type": "tool_start",
                    "name": tc_name,
                    "args_summary": _tool_activity_summary(tc_name, tc_args),
                    "tool_index": tool_idx + 1,
                    "total_tools": total_tools,
                }
                # Attach file_path for file-based tools
                _fp = tc_args.get("path", "")
                if _fp and tc_name in ("read_file", "write_file", "edit_file"):
                    start_event["file_path"] = _fp
                elif tc_name == "search_files":
                    start_event["pattern"] = tc_args.get("pattern", "")
                elif tc_name == "run_command":
                    start_event["command"] = tc_args.get("command", "")[:200]
                await on_tool_event(start_event)

            result_text = await tool_executor(tc_name, tc_args)
            result_preview = _smart_result_preview(tc_name, result_text)
            logger.debug("tool_loop: round %d — %s result (%d chars): %s", loop_count, tc_name, len(result_text), result_preview[:200])
            tool_results.append({"tool_use_id": tc["id"], "content": result_text})

            # Fire structured tool_result event
            if on_tool_event:
                result_event: dict = {
                    "type": "tool_result",
                    "name": tc_name,
                    "result_preview": result_preview,
                    "chars": len(result_text),
                }
                # Carry file_path through for result events too
                _fp = tc_args.get("path", "")
                if _fp and tc_name in ("read_file", "write_file", "edit_file"):
                    result_event["file_path"] = _fp
                if tc_name == "tool_result" or (result_text and "error" in result_text[:50].lower()):
                    result_event["error"] = True
                await on_tool_event(result_event)

        messages.append(LLMMessage(role="user", content="", tool_results=tool_results))

        # --- Doom loop detection ---
        round_sigs = []
        for tc in response.tool_calls:
            sig = tc["name"] + ":" + _hashlib.md5(
                _json.dumps(tc.get("arguments", {}), sort_keys=True).encode()
            ).hexdigest()[:12]
            round_sigs.append(sig)
        _recent_sigs.append(round_sigs)

        if len(_recent_sigs) >= _DOOM_THRESHOLD:
            window = _recent_sigs[-_DOOM_WINDOW:]
            flat = [s for rnd in window for s in rnd]
            from collections import Counter as _Counter
            counts = _Counter(flat)
            repeated = [s.split(":")[0] for s, c in counts.items() if c >= _DOOM_THRESHOLD]
            if repeated:
                logger.warning("tool_loop: doom loop — %s repeated %d+ times in last %d rounds",
                               repeated, _DOOM_THRESHOLD, len(window))
                messages.append(LLMMessage(
                    role="user",
                    content=(
                        f"STOP: You are stuck in a loop calling {', '.join(repeated)} "
                        f"with the same arguments. Try a DIFFERENT approach or call submit_result."
                    ),
                ))
                if on_activity:
                    await on_activity(f"Breaking stuck loop ({', '.join(repeated)})")

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

        # Cancellation check between tool rounds
        if on_cancel_check and await on_cancel_check():
            logger.info("tool_loop: cancelled by caller after round %d", loop_count)
            response.stop_reason = "cancelled"
            break

        if on_activity:
            await on_activity(f"Thinking... (round {loop_count + 1})")

        response = await _retry_on_rate_limit(_call_llm, on_activity=on_activity)

        logger.info(
            "tool_loop: round %d response stop_reason=%s tool_calls=%d",
            loop_count, response.stop_reason,
            len(response.tool_calls) if response.tool_calls else 0,
        )

        if on_activity:
            n_tc = len(response.tool_calls) if response.tool_calls else 0
            if n_tc > 0:
                briefs = [_tool_brief(tc["name"], tc.get("arguments", {})) for tc in response.tool_calls]
                await on_activity(f"Agent will: {', '.join(briefs)}")
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

        # Context overflow guard — compact old tool rounds if approaching limit
        if response.tokens_input > 0:
            ctx_window = get_context_window(response.model or provider.default_model)
            if ctx_window and response.tokens_input > ctx_window * 0.85:
                logger.warning(
                    "tool_loop: context at %.0f%% (%d/%d tokens), compacting",
                    100 * response.tokens_input / ctx_window,
                    response.tokens_input, ctx_window,
                )
                messages[:] = _compact_messages_for_overflow(messages)
                if on_activity:
                    await on_activity("Compacting context (approaching limit)...")

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


def _smart_result_preview(tool_name: str, result_text: str, max_len: int = 500) -> str:
    """Generate a smarter preview of tool results based on tool type."""
    if not result_text:
        return ""

    # For file reads, show more content (useful for review)
    if tool_name == "read_file":
        lines = result_text.split("\n")
        preview = f"({len(lines)} lines) "
        preview += result_text[:max_len]
        if len(result_text) > max_len:
            preview += "..."
        return preview

    # For search results, show match count + first matches
    if tool_name == "search_files":
        lines = result_text.strip().split("\n")
        match_count = len([l for l in lines if l.strip() and not l.startswith("Search")])
        preview = f"({match_count} matches) "
        preview += result_text[:max_len]
        if len(result_text) > max_len:
            preview += "..."
        return preview

    # For write_file, show success/failure concisely
    if tool_name == "write_file":
        if "error" in result_text.lower()[:100]:
            return result_text[:max_len]
        return result_text[:200]

    # For run_command, try to parse exit code
    if tool_name == "run_command":
        try:
            data = _json.loads(result_text)
            exit_code = data.get("exit_code", "?")
            output = data.get("output", "")
            preview = f"(exit {exit_code}) {output[:max_len - 20]}"
            if len(output) > max_len - 20:
                preview += "..."
            return preview
        except Exception:
            pass

    # Default: first N chars
    if len(result_text) > max_len:
        return result_text[:max_len] + "..."
    return result_text


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


def _tool_brief(name: str, args: dict) -> str:
    """Compact tool+arg hint for 'Agent will:' lines, e.g. run_command(npm test)."""
    import os.path as _osp

    if name in ("read_file", "write_file", "edit_file"):
        return f"{name}({_osp.basename(args.get('path', '?'))})"
    if name == "list_directory":
        return f"list_directory({_osp.basename(args.get('path', '?'))}/)"
    if name == "search_files":
        pat = args.get("pattern", "?")
        return f"search_files(\"{pat[:40]}\")"
    if name == "run_command":
        cmd = args.get("command", "?")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        return f"run_command({cmd})"
    if name == "semantic_search":
        q = args.get("query", "?")[:40]
        return f"semantic_search(\"{q}\")"
    return name
