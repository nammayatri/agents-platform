"""Claude Code CLI provider — uses the `claude` CLI as an LLM proxy.

This allows using Anthropic OAuth tokens (sk-ant-oat01-*) which are not
supported by the public Messages API directly.  Claude Code has a
first-party auth channel that accepts OAuth tokens via the
CLAUDE_CODE_OAUTH_TOKEN environment variable, bypassing the public API
limitation.

The CLI is invoked in non-interactive print mode with all built-in tools
disabled (--tools ""), so it acts as a pure LLM passthrough.  Tool
definitions are embedded in the system prompt and tool calls are parsed
from the response text.
"""

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Callable, Awaitable
from uuid import uuid4

from agents.providers.base import AIProvider
from agents.schemas.agent import LLMMessage, LLMResponse, StreamChunk

logger = logging.getLogger(__name__)

# Map model aliases to full IDs (Claude Code accepts both)
MODEL_ALIASES = {
    "sonnet": "sonnet",
    "opus": "opus",
    "haiku": "haiku",
}

# Pricing per 1M tokens (same as Anthropic provider)
PRICING = {
    "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    "claude-haiku-3-20250311": {"input": 0.25, "output": 1.25},
}

CONTEXT_WINDOWS = {
    "claude-sonnet-4-20250514": 200_000,
    "claude-sonnet-4-5-20250929": 200_000,
    "claude-opus-4-20250514": 200_000,
    "claude-haiku-3-20250311": 200_000,
}

# Timeout for a single CLI invocation (seconds)
CLI_TIMEOUT = 300


def _find_claude_cli() -> str | None:
    """Locate the claude CLI binary."""
    return shutil.which("claude")


class ClaudeCodeProvider(AIProvider):
    """AI provider that wraps the Claude Code CLI for OAuth token auth."""

    provider_type = "claude_code"

    def __init__(
        self,
        auth_token: str,
        default_model: str = "sonnet",
        fast_model: str | None = "haiku",
        cli_path: str | None = None,
    ):
        self.auth_token = auth_token
        self.default_model = default_model
        self.fast_model = fast_model
        self.cli_path = cli_path or _find_claude_cli() or "claude"

    def _build_env(self) -> dict[str, str]:
        """Build environment with OAuth token set via first-party channel."""
        env = os.environ.copy()
        # Use CLAUDE_CODE_OAUTH_TOKEN — this routes through Claude Code's
        # first-party auth channel which supports OAuth, unlike
        # ANTHROPIC_AUTH_TOKEN which hits the public Messages API.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = self.auth_token
        # Unset CLAUDECODE to avoid nested-session detection
        env.pop("CLAUDECODE", None)
        # Clear any API key that might override the OAuth token
        env.pop("ANTHROPIC_API_KEY", None)
        return env

    def _build_prompt(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
        tools: list[dict] | None = None,
    ) -> tuple[str | None, str]:
        """Convert messages + tools into a system prompt and user prompt for the CLI.

        Returns (system_prompt, user_prompt).
        """
        sys_parts: list[str] = []
        conversation_parts: list[str] = []

        # Collect system prompt
        for msg in messages:
            if msg.role == "system":
                sys_parts.append(msg.content)

        if system_prompt:
            sys_parts.insert(0, system_prompt)

        # Add tool definitions to system prompt
        if tools:
            tool_defs = self._format_tool_definitions(tools)
            sys_parts.append(tool_defs)

        # Build conversation
        for msg in messages:
            if msg.role == "system":
                continue
            elif msg.role == "user" and msg.tool_results:
                for tr in msg.tool_results:
                    conversation_parts.append(
                        f"<tool_result tool_use_id=\"{tr['tool_use_id']}\">\n"
                        f"{tr['content']}\n"
                        f"</tool_result>"
                    )
            elif msg.role == "assistant":
                if msg.tool_calls:
                    text_parts = []
                    if msg.content:
                        text_parts.append(msg.content)
                    for tc in msg.tool_calls:
                        args_json = json.dumps(tc.get("arguments", {}), indent=2)
                        text_parts.append(
                            f"<tool_use id=\"{tc['id']}\" name=\"{tc['name']}\">\n"
                            f"{args_json}\n"
                            f"</tool_use>"
                        )
                    conversation_parts.append(
                        f"[assistant]\n{''.join(text_parts)}"
                    )
                else:
                    conversation_parts.append(f"[assistant]\n{msg.content}")
            elif msg.role == "user":
                conversation_parts.append(f"[user]\n{msg.content}")

        combined_system = "\n\n".join(sys_parts) if sys_parts else None
        user_prompt = "\n\n".join(conversation_parts)

        return combined_system, user_prompt

    @staticmethod
    def _format_tool_definitions(tools: list[dict]) -> str:
        """Format tool definitions as XML for the system prompt."""
        tool_names = [t["name"] for t in tools]
        parts = [
            "IMPORTANT: You are operating as a pure LLM backend. "
            "You MUST ONLY use the tools listed below. Do NOT use Read, Write, Edit, "
            "Bash, Glob, Grep, WebFetch, WebSearch, Task, or any other tools not "
            "explicitly defined here. Only these tools exist: "
            f"{', '.join(tool_names)}.\n\n"
            "When you want to use a tool, respond with a tool_use XML block:\n"
            "<tool_use id=\"unique_id\" name=\"tool_name\">\n"
            "{\"param\": \"value\"}\n"
            "</tool_use>\n\n"
            "Available tools:"
        ]
        for t in tools:
            name = t["name"]
            desc = t.get("description", "")
            params = t.get("parameters", t.get("input_schema", {}))
            parts.append(
                f"\n<tool_definition name=\"{name}\">\n"
                f"Description: {desc}\n"
                f"Parameters: {json.dumps(params, indent=2)}\n"
                f"</tool_definition>"
            )
        return "\n".join(parts)

    async def _run_cli(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int = 4096,
    ) -> dict:
        """Invoke the claude CLI and return the parsed JSON response."""
        cmd = [
            self.cli_path,
            "-p",
            "--output-format", "json",
            "--no-session-persistence",
            "--max-turns", "1",
            # Suppress Claude Code's built-in tools so the model only
            # sees the app's tools (defined in the system prompt text).
            "--allowedTools", "",
        ]

        use_model = model or self.default_model
        cmd.extend(["--model", use_model])

        # Always override Claude Code's default system prompt so it acts
        # as a pure LLM passthrough — no agentic behavior, no built-in
        # tool definitions injected.
        cmd.extend([
            "--system-prompt",
            system_prompt or "You are a helpful assistant.",
        ])

        logger.info(
            "claude_code: invoking CLI model=%s prompt_len=%d sys_len=%d",
            use_model, len(prompt), len(system_prompt or ""),
        )

        # Write stdout to a temp file to avoid pipe buffer truncation.
        # asyncio.subprocess.PIPE was observed to truncate output at 8191
        # bytes; writing to a file has no such limit.
        tmp_path = None
        try:
            fd, tmp_path = tempfile.mkstemp(
                suffix=".json", prefix="claude_out_"
            )
            stdout_file = os.fdopen(fd, "wb")

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=stdout_file,
                stderr=asyncio.subprocess.PIPE,
                env=self._build_env(),
            )

            try:
                _, stderr = await asyncio.wait_for(
                    proc.communicate(input=prompt.encode("utf-8")),
                    timeout=CLI_TIMEOUT,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise TimeoutError(f"Claude CLI timed out after {CLI_TIMEOUT}s")
            finally:
                stdout_file.close()

            # Read full output from temp file (no pipe buffer limit)
            with open(tmp_path, "r", encoding="utf-8", errors="replace") as f:
                stdout_text = f.read().strip()

            stderr_text = stderr.decode("utf-8", errors="replace").strip()
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

        if proc.returncode != 0:
            logger.error(
                "claude_code: CLI exited %d stderr=%s stdout=%s",
                proc.returncode, stderr_text[:500], stdout_text[:500],
            )
            raise RuntimeError(
                f"Claude CLI exited with code {proc.returncode}: "
                f"{stderr_text[:500] or stdout_text[:500]}"
            )

        # Parse JSON response — the CLI may emit debug/warning text before
        # or after the JSON object, so we use brace-depth matching to
        # extract the outermost {...} reliably.
        result = self._extract_json_object(stdout_text)
        if result is None:
            raise ValueError(
                f"Failed to parse CLI JSON output ({len(stdout_text)} bytes): "
                f"{stdout_text[:300]}"
            )

        if result.get("is_error"):
            raise RuntimeError(
                f"Claude CLI error: {result.get('result', 'unknown error')}"
            )

        return result

    @staticmethod
    def _extract_json_object(text: str) -> dict | None:
        """Extract the outermost JSON object from text using brace-depth matching.

        Handles cases where there is extra content before/after the JSON.
        """
        start = text.find("{")
        if start < 0:
            return None

        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            c = text[i]
            if escape:
                escape = False
                continue
            if c == "\\":
                if in_string:
                    escape = True
                continue
            if c == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start : i + 1])
                    except json.JSONDecodeError:
                        return None
        return None

    @staticmethod
    def _parse_tool_calls(content: str) -> tuple[str, list[dict]]:
        """Parse tool_use XML blocks from response text.

        Handles two formats:
        1. JSON args:  <tool_use id="..." name="...">{"key":"val"}</tool_use>
        2. Parameter tags (Claude Code native):
           <tool_use id="..." name="...">
             <parameter name="key">value</parameter>
           </tool_use>

        Returns (cleaned_content, tool_calls).
        """
        tool_calls = []

        # Match <tool_use id="..." name="...">...body...</tool_use>
        pattern = re.compile(
            r'<tool_use\s+id="([^"]*?)"\s+name="([^"]*?)">\s*(.*?)\s*</tool_use>',
            re.DOTALL,
        )
        for match in pattern.finditer(content):
            tc_id = match.group(1) or f"tc_{uuid4().hex[:8]}"
            tc_name = match.group(2)
            body = match.group(3).strip()

            # Try JSON args first
            args = None
            if body.startswith("{"):
                try:
                    args = json.loads(body)
                except (json.JSONDecodeError, TypeError):
                    pass

            # Fallback: parse <parameter name="key">value</parameter> tags
            if args is None and "<parameter" in body:
                args = {}
                for pm in re.finditer(
                    r'<parameter\s+name="([^"]*?)">(.*?)</parameter>',
                    body,
                    re.DOTALL,
                ):
                    args[pm.group(1)] = pm.group(2)

            if args is None:
                args = {"_raw": body} if body else {}

            tool_calls.append({
                "id": tc_id,
                "name": tc_name,
                "arguments": args,
            })

        # Also handle incomplete/unclosed tool_use blocks (truncated responses)
        if not tool_calls:
            # Check for unclosed <tool_use> blocks
            unclosed = re.compile(
                r'<tool_use\s+id="([^"]*?)"\s+name="([^"]*?)">',
            )
            for match in unclosed.finditer(content):
                tc_id = match.group(1) or f"tc_{uuid4().hex[:8]}"
                tc_name = match.group(2)
                # Extract everything after this opening tag until the next
                # <tool_use> or end of string
                start = match.end()
                next_match = unclosed.search(content, start)
                body = content[start : next_match.start() if next_match else len(content)].strip()

                args = {}
                for pm in re.finditer(
                    r'<parameter\s+name="([^"]*?)">(.*?)</parameter>',
                    body,
                    re.DOTALL,
                ):
                    args[pm.group(1)] = pm.group(2)

                if not args and body:
                    try:
                        args = json.loads(body)
                    except (json.JSONDecodeError, TypeError):
                        args = {"_raw": body}

                tool_calls.append({
                    "id": tc_id,
                    "name": tc_name,
                    "arguments": args,
                })

        # Remove tool_use blocks from content (both closed and unclosed)
        cleaned = pattern.sub("", content)
        cleaned = re.sub(
            r'<tool_use\s+id="[^"]*?"\s+name="[^"]*?">.*',
            "", cleaned, flags=re.DOTALL,
        )
        return cleaned.strip(), tool_calls

    def _extract_usage(self, result: dict) -> dict:
        """Extract token usage from CLI JSON response."""
        usage = result.get("usage", {})
        model_usage = result.get("modelUsage", {})

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cached = usage.get("cache_read_input_tokens", 0)

        # Fallback: sum from modelUsage
        if not input_tokens and model_usage:
            for mu in model_usage.values():
                input_tokens += mu.get("inputTokens", 0)
                output_tokens += mu.get("outputTokens", 0)
                cached += mu.get("cacheReadInputTokens", 0)

        return {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached,
        }

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
        sys_prompt, user_prompt = self._build_prompt(
            messages, system_prompt=system_prompt, tools=tools,
        )

        result = await self._run_cli(
            user_prompt,
            model=model or self.default_model,
            system_prompt=sys_prompt,
            max_tokens=max_tokens,
        )

        raw_content = result.get("result", "")
        usage = self._extract_usage(result)
        cost = result.get("total_cost_usd", 0.0) or result.get("cost_usd", 0.0)

        # Parse tool calls from the response text
        tool_calls = []
        content = raw_content
        if tools and raw_content:
            content, tool_calls = self._parse_tool_calls(raw_content)
            # Filter out any Claude Code native tool calls that leaked
            # through (e.g., Read, Write, Edit, Bash, Glob, Grep).
            # Only keep tool calls that match the app's tool definitions.
            app_tool_names = {t["name"] for t in tools}
            tool_calls = [tc for tc in tool_calls if tc["name"] in app_tool_names]

        stop_reason = result.get("stop_reason", "end_turn") or "end_turn"
        if tool_calls:
            stop_reason = "tool_use"

        used_model = model or self.default_model
        # Try to get actual model from modelUsage
        model_usage = result.get("modelUsage", {})
        if model_usage:
            used_model = next(iter(model_usage.keys()), used_model)

        logger.info(
            "claude_code: response content_len=%d tool_calls=%d cost=%.4f tokens_in=%d tokens_out=%d",
            len(content), len(tool_calls), cost,
            usage["input_tokens"], usage["output_tokens"],
        )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=usage["input_tokens"],
            tokens_output=usage["output_tokens"],
            model=used_model,
            stop_reason=stop_reason,
            cost_usd=cost,
            cached_tokens=usage["cached_tokens"],
        )

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
        # Claude Code CLI with --output-format stream-json can stream,
        # but for simplicity we use the non-streaming path and emit
        # the full content as a single token callback.
        response = await self.send_message(
            messages,
            model=model,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
            tool_choice=tool_choice,
        )
        if response.content:
            await on_token(response.content)
        return response

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
        # Use non-streaming and yield the result as a single chunk
        response = await self.send_message(
            messages,
            model=model,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            system_prompt=system_prompt,
            tool_choice=tool_choice,
        )
        if response.content:
            yield StreamChunk(delta=response.content, chunk_type="text")

    async def health_check(self) -> bool:
        try:
            return await self.health_check_detailed()
        except Exception:
            return False

    async def health_check_detailed(self) -> bool:
        """Verify the CLI is available and the token works."""
        # First check CLI exists
        if not _find_claude_cli():
            raise RuntimeError("claude CLI not found in PATH")

        result = await self._run_cli(
            "respond with just the word pong",
            model=self.fast_model or self.default_model,
        )
        return bool(result.get("result"))

    def estimate_cost(self, tokens_input: int, tokens_output: int, model: str) -> float:
        pricing = PRICING.get(model, {"input": 3.0, "output": 15.0})
        return (tokens_input * pricing["input"] + tokens_output * pricing["output"]) / 1_000_000

    def get_context_window(self, model: str | None = None) -> int:
        return CONTEXT_WINDOWS.get(model or self.default_model, 200_000)

    async def list_models(self) -> list[dict]:
        known = [
            {"id": "sonnet", "name": "Claude Sonnet (latest)"},
            {"id": "opus", "name": "Claude Opus (latest)"},
            {"id": "haiku", "name": "Claude Haiku (latest)"},
            {"id": "claude-sonnet-4-20250514", "name": "Claude Sonnet 4"},
            {"id": "claude-opus-4-20250514", "name": "Claude Opus 4"},
            {"id": "claude-haiku-3-20250311", "name": "Claude Haiku 3"},
        ]
        return [
            {**m, "is_default": m["id"] == self.default_model}
            for m in known
        ]
