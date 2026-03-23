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

    def get_context_window(self, model: str | None = None) -> int:
        """Return the context window size (in tokens) for the given model.

        Subclasses should override with their actual model metadata.
        Default returns a conservative 128k.
        """
        return 128_000

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
    summary = "Previous exploration summary (compacted — re-read files or re-run tools if you need details):\n" + "\n".join(summary_parts[-20:])
    return [*head, LLMMessage(role="user", content=summary), *tail]


async def _compact_messages_with_llm(
    provider: AIProvider,
    messages: list[LLMMessage],
    keep_recent: int = 6,
) -> list[LLMMessage]:
    """Use a fast LLM to summarize old tool interactions for context compaction.

    Preserves: what was attempted, files modified, decisions made, errors hit.
    Falls back to string-based compaction on any failure.
    """
    head: list[LLMMessage] = []
    for m in messages:
        head.append(m)
        if m.role == "user" and not m.tool_results:
            break
    if len(messages) <= len(head) + keep_recent:
        return messages

    tail = messages[-keep_recent:]
    middle = messages[len(head):-keep_recent]

    if not middle:
        return messages

    # Build a text representation of what happened in the middle section
    middle_text_parts: list[str] = []
    for m in middle:
        if m.role == "assistant" and m.tool_calls:
            for tc in m.tool_calls:
                middle_text_parts.append(f"Called {tc['name']}({', '.join(f'{k}={repr(v)[:80]}' for k, v in _normalize_tool_args(tc.get('arguments', {})).items())})")
        elif m.role == "user" and m.tool_results:
            for tr in m.tool_results:
                preview = tr.get("content", "")[:200]
                middle_text_parts.append(f"Result: {preview}")
        elif m.content:
            middle_text_parts.append(f"[{m.role}]: {m.content[:200]}")

    middle_text = "\n".join(middle_text_parts)
    if not middle_text.strip():
        return _compact_messages_for_overflow(messages, keep_recent)

    try:
        summary_prompt = (
            "Summarize the following agent tool interactions concisely. "
            "Preserve:\n"
            "- What was attempted and why\n"
            "- Files read, created, or modified (with paths)\n"
            "- Key decisions made\n"
            "- Errors encountered and how they were resolved\n"
            "- Current state of the task\n\n"
            "Be concise but complete. Output only the summary.\n\n"
            f"Tool interactions:\n{middle_text}"
        )

        fast_model = provider.fast_model or provider.default_model
        summary_response = await provider.send_message(
            [LLMMessage(role="user", content=summary_prompt)],
            model=fast_model,
            max_tokens=1024,
            temperature=0.0,
        )
        summary = summary_response.content
        if not summary or len(summary.strip()) < 20:
            raise ValueError("Summary too short")

        compacted = list(head)
        compacted.append(LLMMessage(
            role="user",
            content=f"[CONTEXT COMPACTED — earlier tool interactions summarized. Re-read files if you need exact content.]\n{summary}",
        ))
        compacted.extend(tail)

        logger.info(
            "compact_llm: reduced %d messages to %d (summary: %d chars)",
            len(messages), len(compacted), len(summary),
        )
        return compacted
    except Exception as exc:
        logger.warning("compact_llm: failed (%s), falling back to string compaction", exc)
        return _compact_messages_for_overflow(messages, keep_recent)


_RETRIABLE_STATUS_CODES = frozenset({429, 503, 529})


def _extract_retry_after(exc: Exception) -> float | None:
    """Extract retry delay from exception headers, supporting multiple formats.

    Handles:
    - ``retry-after`` header (seconds or HTTP-date)
    - ``retry-after-ms`` header (milliseconds)
    - ``x-ratelimit-reset-requests`` / ``x-ratelimit-reset-tokens``
    - Anthropic/OpenAI SDK ``retry_after`` attribute
    """
    # 1. SDK-level attribute (e.g. anthropic.RateLimitError.retry_after)
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is not None:
        try:
            return max(float(retry_after), 0.5)
        except (TypeError, ValueError):
            pass

    # 2. Response headers
    hdrs: dict = {}
    # httpx-based SDKs attach the response object
    resp = getattr(exc, "response", None)
    if resp is not None:
        hdrs = dict(getattr(resp, "headers", {}) or {})
    if not hdrs:
        hdrs = dict(getattr(exc, "headers", {}) or {})
    if not hdrs:
        return None

    # Normalize header names to lowercase
    hdrs_lower = {k.lower(): v for k, v in hdrs.items()}

    # retry-after-ms (milliseconds — Anthropic)
    ms_val = hdrs_lower.get("retry-after-ms")
    if ms_val is not None:
        try:
            return max(float(ms_val) / 1000.0, 0.5)
        except (TypeError, ValueError):
            pass

    # retry-after (seconds or HTTP-date — standard)
    ra_val = hdrs_lower.get("retry-after")
    if ra_val is not None:
        try:
            return max(float(ra_val), 0.5)
        except (TypeError, ValueError):
            pass
        # Could be an HTTP-date — parse with email.utils
        try:
            from email.utils import parsedate_to_datetime
            import time as _time

            target = parsedate_to_datetime(ra_val).timestamp()
            return max(target - _time.time(), 0.5)
        except Exception:
            pass

    # x-ratelimit-reset-requests / x-ratelimit-reset-tokens (seconds until reset)
    for key in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        val = hdrs_lower.get(key)
        if val is not None:
            try:
                # Format can be "2s", "1m30s", or plain seconds
                parsed = _parse_duration(val)
                if parsed is not None:
                    return max(parsed, 0.5)
            except Exception:
                pass

    return None


def _parse_duration(val: str) -> float | None:
    """Parse a duration string like '2s', '1m30s', '500ms', or plain number."""
    import re as _re

    val = val.strip()
    # Plain number
    try:
        return float(val)
    except ValueError:
        pass
    # Duration format: 1m30s, 2s, 500ms
    total = 0.0
    for amount, unit in _re.findall(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h)?", val):
        n = float(amount)
        match unit:
            case "ms":
                total += n / 1000.0
            case "s" | "":
                total += n
            case "m":
                total += n * 60.0
            case "h":
                total += n * 3600.0
    return total if total > 0 else None


def _classify_retriable(exc: Exception) -> bool:
    """Determine if an exception is retriable based on status code and error message."""
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status in _RETRIABLE_STATUS_CODES:
        return True
    err_s = str(exc).lower()
    if "rate" in err_s and "limit" in err_s:
        return True
    if "overloaded" in err_s:
        return True
    if status == 500 and "internal server error" in err_s:
        return True
    return False


async def _retry_on_rate_limit(
    call: Callable[[], Awaitable[Any]],
    *,
    max_retries: int = 3,
    on_activity: ActivityCallback | None = None,
) -> Any:
    """Retry an async call on rate-limit (429) or overloaded (529/503) errors.

    Respects provider-specific ``retry-after`` headers when available,
    otherwise uses exponential backoff (5s, 10s, 20s… capped at 60s).
    """
    import asyncio as _asyncio

    for attempt in range(max_retries + 1):
        try:
            return await call()
        except Exception as e:
            if _classify_retriable(e) and attempt < max_retries:
                wait = _extract_retry_after(e)
                if wait is None:
                    wait = min(2 ** attempt * 5, 60)
                logger.warning(
                    "Rate limit hit (attempt %d/%d), waiting %.1fs",
                    attempt + 1, max_retries, wait,
                )
                if on_activity:
                    await on_activity(f"Rate limited, retrying in {wait:.0f}s...")
                await _asyncio.sleep(wait)
                continue
            raise


class _DoomLoopState:
    """Tracks doom loop escalation across tool rounds.

    Escalation levels:
      0 — No issue detected.
      1 — Warning: same tool+args repeated 3+ times in 5 rounds.
      2 — Correction: looping continued after warning (tools will be removed).
      3 — Hard stop: looping continued after correction.
    """

    _WINDOW = 5
    _THRESHOLD = 3

    def __init__(self) -> None:
        self._recent_sigs: list[list[str]] = []
        self._escalation: int = 0
        self._repeated: list[str] = []

    def record_round(self, tool_calls: list[dict]) -> None:
        """Record tool call signatures for this round."""
        sigs = []
        for tc in tool_calls:
            sig = tc["name"] + ":" + _hashlib.md5(
                _json.dumps(tc.get("arguments", {}), sort_keys=True).encode()
            ).hexdigest()[:12]
            sigs.append(sig)
        self._recent_sigs.append(sigs)

    def check(self) -> int:
        """Check for doom loop and return escalation level (0-3)."""
        if len(self._recent_sigs) < self._THRESHOLD:
            return 0

        from collections import Counter as _Counter

        window = self._recent_sigs[-self._WINDOW:]
        flat = [s for rnd in window for s in rnd]
        counts = _Counter(flat)
        repeated = [s.split(":")[0] for s, c in counts.items() if c >= self._THRESHOLD]

        if not repeated:
            # No repetition — reset escalation
            self._escalation = 0
            self._repeated = []
            return 0

        self._repeated = repeated
        self._escalation = min(self._escalation + 1, 3)
        return self._escalation

    @property
    def repeated_tools(self) -> list[str]:
        """Tool names currently stuck in a loop."""
        return self._repeated


_SIDE_EFFECT_FREE_TOOLS = frozenset({
    "read_file", "list_directory", "search_files", "semantic_search",
})


async def _execute_with_cancel(
    coro_fn,
    cancel_event: "asyncio.Event",
    cancel_check=None,
    poll_interval: float = 1.0,
):
    """Run coro_fn() but return None if cancel_event fires.

    When cancel_check is provided, polls it every poll_interval seconds;
    True return sets cancel_event automatically.
    """
    import asyncio as _aio

    poller = None
    if cancel_check is not None:
        async def _poll():
            try:
                while not cancel_event.is_set():
                    if await cancel_check():
                        cancel_event.set()
                        return
                    await _aio.sleep(poll_interval)
            except _aio.CancelledError:
                pass
        poller = _aio.ensure_future(_poll())

    exec_task = _aio.ensure_future(coro_fn())
    wait_task = _aio.ensure_future(cancel_event.wait())

    try:
        done, pending = await _aio.wait(
            {exec_task, wait_task},
            return_when=_aio.FIRST_COMPLETED,
        )
    finally:
        if poller is not None:
            poller.cancel()
        for t in (exec_task, wait_task):
            if t not in done:
                t.cancel()
        # Suppress cancellation errors on cleanup
        for t in (exec_task, wait_task):
            if t not in (done if 'done' in dir() else set()):
                try:
                    await t
                except (_aio.CancelledError, Exception):
                    pass
        if poller:
            try:
                await poller
            except (_aio.CancelledError, Exception):
                pass

    if exec_task in done:
        return exec_task.result()
    return None  # Cancelled


# Maximum tool result size kept in the LLM conversation messages.
# Larger results are truncated before being appended.  The full result
# is still logged and emitted as events — this only affects what the
# LLM sees on subsequent rounds.  ~15K chars ≈ 4K tokens.
MAX_TOOL_RESULT_IN_CONTEXT = 15_000


def _truncate_tool_result(text: str) -> str:
    """Truncate a tool result but tell the LLM what was cut and how to get more."""
    if len(text) <= MAX_TOOL_RESULT_IN_CONTEXT:
        return text
    total = len(text)
    lines = text.split("\n")
    kept: list[str] = []
    char_count = 0
    for line in lines:
        if char_count + len(line) + 1 > MAX_TOOL_RESULT_IN_CONTEXT:
            break
        kept.append(line)
        char_count += len(line) + 1
    remaining_lines = len(lines) - len(kept)
    result = "\n".join(kept)
    result += (
        f"\n\n... (showing {len(kept)} of {len(lines)} lines, {MAX_TOOL_RESULT_IN_CONTEXT} of {total} chars. "
        f"{remaining_lines} lines not shown. "
        f"Re-read with offset={len(kept)} if you need the rest.)"
    )
    return result


def _trim_old_tool_results(messages: list[LLMMessage], keep_full: int = 6) -> None:
    """Trim tool results in older messages to short previews.

    Keeps the most recent ``keep_full`` tool-result messages at full size.
    Older ones are trimmed to 400 chars per result with a hint to re-request,
    freeing memory from file contents / search outputs the LLM no longer
    needs in detail.
    """
    tr_indices = [i for i, m in enumerate(messages) if m.tool_results]
    if len(tr_indices) <= keep_full:
        return
    for idx in tr_indices[:-keep_full]:
        for tr in messages[idx].tool_results:
            content = tr.get("content", "")
            if len(content) > 500:
                preview = content[:400]
                total = len(content)
                tr["content"] = (
                    f"{preview}\n\n"
                    f"... (trimmed from {total} chars — this is an older result. "
                    f"Re-read the file or re-run the tool if you need the full content.)"
                )


async def _execute_tools_parallel(
    tool_calls: list[dict],
    tool_executor: ToolExecutor,
    known_tool_names: set[str],
    *,
    on_activity: ActivityCallback | None = None,
    on_tool_event: ToolEventCallback | None = None,
    cancel_event: "asyncio.Event | None" = None,
    on_cancel_check: Callable[[], Awaitable[bool]] | None = None,
    max_parallel: int = 5,
) -> tuple[list[dict], bool]:
    """Execute tool calls with parallelism for read-only tools.

    Read-only tools (read_file, list_directory, search_files, semantic_search)
    run concurrently. Write tools run sequentially after reads complete.

    Returns:
        (tool_results, cancelled) — list of tool result dicts and whether cancelled.
    """
    import asyncio as _aio

    reads = []
    writes = []
    for tc in tool_calls:
        if tc["name"] in _SIDE_EFFECT_FREE_TOOLS:
            reads.append(tc)
        else:
            writes.append(tc)

    tool_results = []
    cancelled = False

    # Execute read-only tools concurrently
    if reads:
        sem = _aio.Semaphore(max_parallel)

        async def _run_read(tc):
            tc_name = tc["name"]
            tc_args = _normalize_tool_args(tc.get("arguments", {}))

            # Validate
            if known_tool_names and tc_name not in known_tool_names:
                return {"tool_use_id": tc["id"], "content": _json.dumps({
                    "error": f"Tool '{tc_name}' does not exist. Available tools: {', '.join(sorted(known_tool_names))}",
                })}

            if on_tool_event:
                await on_tool_event({"type": "tool_start", "name": tc_name, "args_summary": _tool_activity_summary(tc_name, tc_args), "parallel": True})

            async with sem:
                if cancel_event and cancel_event.is_set():
                    return {"tool_use_id": tc["id"], "content": _json.dumps({"error": "Cancelled"})}
                result_text = await tool_executor(tc_name, tc_args)

            if on_tool_event:
                await on_tool_event({"type": "tool_result", "name": tc_name, "result_preview": _smart_result_preview(tc_name, result_text), "chars": len(result_text), "parallel": True})

            return {"tool_use_id": tc["id"], "content": _truncate_tool_result(result_text)}

        read_results = await _aio.gather(*[_run_read(tc) for tc in reads], return_exceptions=True)
        for i, r in enumerate(read_results):
            if isinstance(r, Exception):
                tool_results.append({"tool_use_id": reads[i]["id"], "content": _json.dumps({"error": str(r)})})
            else:
                tool_results.append(r)

        if on_activity and len(reads) > 1:
            await on_activity(f"Executed {len(reads)} read tools in parallel")

    # Execute write tools sequentially
    for tc in writes:
        if cancel_event and cancel_event.is_set():
            cancelled = True
            break

        tc_name = tc["name"]
        tc_args = tc.get("arguments", {})

        if known_tool_names and tc_name not in known_tool_names:
            tool_results.append({"tool_use_id": tc["id"], "content": _json.dumps({
                "error": f"Tool '{tc_name}' does not exist. Available tools: {', '.join(sorted(known_tool_names))}",
            })})
            continue

        if on_activity:
            await on_activity(_tool_activity_summary(tc_name, tc_args))
        if on_tool_event:
            start_event = {"type": "tool_start", "name": tc_name, "args_summary": _tool_activity_summary(tc_name, tc_args)}
            if tc_name in ("read_file", "write_file", "edit_file"):
                start_event["file_path"] = tc_args.get("path", "")
            await on_tool_event(start_event)

        if cancel_event is not None:
            _result = await _execute_with_cancel(
                lambda _n=tc_name, _a=tc_args: tool_executor(_n, _a),
                cancel_event,
                cancel_check=on_cancel_check,
            )
            if _result is None:
                cancelled = True
                break
            result_text = _result
        else:
            result_text = await tool_executor(tc_name, tc_args)

        if on_tool_event:
            await on_tool_event({"type": "tool_result", "name": tc_name, "result_preview": _smart_result_preview(tc_name, result_text), "chars": len(result_text)})

        tool_results.append({"tool_use_id": tc["id"], "content": _truncate_tool_result(result_text)})

    return tool_results, cancelled


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
    parallel_tools: bool = True,
    compaction_strategy: str = "llm",  # "llm" or "string"
    cancel_event: "asyncio.Event | None" = None,
    nudge_tools: bool = True,
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

    logger.debug(
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

    import asyncio as _asyncio_mod
    if cancel_event is None and on_cancel_check is not None:
        cancel_event = _asyncio_mod.Event()

    content_parts: list[str] = []
    tools_called: list[str] = []      # track tool names across rounds
    total_tool_calls = 0
    loop_count = 0
    nudged = False
    doom = _DoomLoopState()

    # Nudge: if tools were provided but the model wrote text instead of calling
    # them, inject a correction and retry once.  This catches models (e.g.
    # kimi-latest) that describe what they *would* do instead of acting.
    # Disabled for interactive chat modes where a direct text answer is valid.
    if nudge_tools and tools and not response.tool_calls and response.content:
        logger.debug("tool_loop: model produced text without tool calls, nudging")
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
        logger.debug(
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
        logger.debug("tool_loop: round %d/%d — calling tools: %s", loop_count, max_rounds, tool_names)

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

        if parallel_tools and len(response.tool_calls) > 1:
            tool_results, was_cancelled = await _execute_tools_parallel(
                response.tool_calls,
                tool_executor,
                known_tool_names,
                on_activity=on_activity,
                on_tool_event=on_tool_event,
                cancel_event=cancel_event,
                on_cancel_check=on_cancel_check,
            )
            if was_cancelled:
                response.stop_reason = "cancelled"
                if tool_results:
                    messages.append(LLMMessage(role="user", content="", tool_results=tool_results))
                break
        else:
            tool_results = []
            total_tools = len(response.tool_calls)
            for tool_idx, tc in enumerate(response.tool_calls):
                if cancel_event is not None and cancel_event.is_set():
                    response.stop_reason = "cancelled"
                    break

                tc_name = tc["name"]
                tc_args = _normalize_tool_args(tc.get("arguments", {}))
                logger.debug("tool_loop: round %d — exec %s args_keys=%s", loop_count, tc_name, list(tc_args.keys()))

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

                if cancel_event is not None:
                    _result = await _execute_with_cancel(
                        lambda: tool_executor(tc_name, tc_args),
                        cancel_event,
                        cancel_check=on_cancel_check,
                    )
                    if _result is None:
                        logger.debug("tool_loop: tool %s cancelled mid-execution", tc_name)
                        response.stop_reason = "cancelled"
                        break
                    result_text = _result
                else:
                    result_text = await tool_executor(tc_name, tc_args)
                result_preview = _smart_result_preview(tc_name, result_text)
                logger.debug("tool_loop: round %d — %s result (%d chars): %s", loop_count, tc_name, len(result_text), result_preview[:200])
                tool_results.append({"tool_use_id": tc["id"], "content": _truncate_tool_result(result_text)})

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

            if response.stop_reason == "cancelled":
                if tool_results:
                    messages.append(LLMMessage(role="user", content="", tool_results=tool_results))
                break

        messages.append(LLMMessage(role="user", content="", tool_results=tool_results))

        # Trim older tool results to save memory (keep recent ones full)
        _trim_old_tool_results(messages, keep_full=6)

        # --- Doom loop detection with escalation ---
        doom.record_round(response.tool_calls)
        doom_level = doom.check()
        if doom_level >= 3:
            # Level 3: hard stop — agent ignored warn + correction
            logger.error("tool_loop: doom loop HARD STOP — %s after escalation", doom.repeated_tools)
            response.stop_reason = "doom_loop"
            if on_activity:
                await on_activity(f"HARD STOP: stuck loop ({', '.join(doom.repeated_tools)})")
            if on_tool_event:
                await on_tool_event({
                    "type": "doom_loop",
                    "level": 3,
                    "tools": doom.repeated_tools,
                })
            break
        elif doom_level == 2:
            # Level 2: correct — remove the looping tools from next LLM call
            logger.warning("tool_loop: doom loop escalation L2 — removing %s from tools", doom.repeated_tools)
            looping_names = set(doom.repeated_tools)
            send_kwargs["tools"] = [t for t in (tools or []) if t["name"] not in looping_names]
            messages.append(LLMMessage(
                role="user",
                content=(
                    f"STOP: You are STILL calling {', '.join(doom.repeated_tools)} with the same arguments "
                    f"after being warned. Those tools have been temporarily removed. "
                    f"Use a COMPLETELY DIFFERENT approach or call task_complete/submit_result."
                ),
            ))
            if on_activity:
                await on_activity(f"Removing stuck tools ({', '.join(doom.repeated_tools)})")
        elif doom_level == 1:
            # Level 1: warn — inject correction message (existing behavior)
            logger.warning("tool_loop: doom loop warning — %s", doom.repeated_tools)
            messages.append(LLMMessage(
                role="user",
                content=(
                    f"STOP: You are stuck in a loop calling {', '.join(doom.repeated_tools)} "
                    f"with the same arguments. Try a DIFFERENT approach or call submit_result."
                ),
            ))
            if on_activity:
                await on_activity(f"Breaking stuck loop ({', '.join(doom.repeated_tools)})")
        elif doom_level == 0 and "tools" in send_kwargs and send_kwargs["tools"] != tools:
            # Restore original tools after doom loop clears (post level 2 correction)
            if tools:
                send_kwargs["tools"] = tools

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
                logger.debug("tool_loop: injected %d user message(s) before round %d",
                            len(injected_parts), loop_count + 1)

        # Cancellation check between tool rounds
        if on_cancel_check and await on_cancel_check():
            if cancel_event is not None:
                cancel_event.set()
            logger.debug("tool_loop: cancelled by caller after round %d", loop_count)
            response.stop_reason = "cancelled"
            break

        if on_activity:
            await on_activity(f"Thinking... (round {loop_count + 1})")

        response = await _retry_on_rate_limit(_call_llm, on_activity=on_activity)

        logger.debug(
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
            ctx_window = provider.get_context_window(response.model or provider.default_model)
            if ctx_window and response.tokens_input > ctx_window * 0.60:
                logger.warning(
                    "tool_loop: context at %.0f%% (%d/%d tokens), compacting",
                    100 * response.tokens_input / ctx_window,
                    response.tokens_input, ctx_window,
                )
                if compaction_strategy == "llm":
                    messages[:] = await _compact_messages_with_llm(provider, messages)
                else:
                    messages[:] = _compact_messages_for_overflow(messages)
                if on_activity:
                    await on_activity("Compacting context (approaching limit)...")
            elif loop_count > 0 and loop_count % 15 == 0 and len(messages) > 10:
                # Periodic compaction by message count to prevent unbounded growth
                logger.info("tool_loop: periodic compaction at round %d (%d messages)", loop_count, len(messages))
                messages[:] = _compact_messages_for_overflow(messages)
                if on_activity:
                    await on_activity("Compacting context (periodic cleanup)...")

    truncated = loop_count >= max_rounds and bool(response.tool_calls)
    if truncated:
        logger.warning("tool_loop: hit max_rounds=%d, stopping with pending tool calls", max_rounds)
        response.stop_reason = "max_tool_rounds"

    logger.debug("tool_loop: finished after %d rounds, final content_len=%d", loop_count, len(response.content or ""))

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

    # Free the (potentially large) conversation messages accumulated during the
    # tool loop.  Callers only need the returned content + response.
    messages.clear()
    content_parts.clear()

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


def _normalize_tool_args(raw: object) -> dict:
    """Ensure tool arguments are a dict — some providers return a JSON string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            import json as _j
            parsed = _j.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


def _tool_activity_summary(name: str, args: dict | str) -> str:
    """One-line human-readable summary of a tool call for the activity log."""
    args = _normalize_tool_args(args)
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


def _tool_brief(name: str, args: dict | str) -> str:
    """Compact tool+arg hint for 'Agent will:' lines, e.g. run_command(npm test)."""
    args = _normalize_tool_args(args)
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
