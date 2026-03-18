import json as _json_mod
import logging
from collections.abc import AsyncIterator, Callable, Awaitable

import openai

from agents.providers.base import AIProvider
from agents.schemas.agent import LLMMessage, LLMResponse, StreamChunk

logger = logging.getLogger(__name__)

PRICING = {
    "gpt-4o": {"input": 2.50, "output": 10.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "o3": {"input": 10.0, "output": 40.0},
    "o3-mini": {"input": 1.10, "output": 4.40},
}


class OpenAIProvider(AIProvider):
    provider_type = "openai"

    def __init__(
        self,
        api_key: str,
        default_model: str = "gpt-4o",
        fast_model: str | None = "gpt-4o-mini",
    ):
        self.client = openai.AsyncOpenAI(api_key=api_key)
        self.default_model = default_model
        self.fast_model = fast_model

    def _build_messages(
        self, messages: list[LLMMessage], system_prompt: str | None = None
    ) -> list[dict]:
        import json as _json

        api_messages = []
        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
                # OpenAI assistant message with tool_calls
                tc_list = []
                for tc in msg.tool_calls:
                    tc_list.append({
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": _json.dumps(tc.get("arguments", {})),
                        },
                    })
                api_messages.append({
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": tc_list,
                })
            elif msg.tool_results:
                # OpenAI uses separate "tool" role messages for each result
                for tr in msg.tool_results:
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_use_id"],
                        "content": tr["content"],
                    })
            else:
                api_messages.append({"role": msg.role, "content": msg.content})
        if system_prompt:
            api_messages.insert(0, {"role": "system", "content": system_prompt})
        return api_messages

    @staticmethod
    def _make_strict_schema(schema: dict) -> dict:
        """Transform a JSON Schema for OpenAI strict mode.

        Recursively adds ``additionalProperties: false`` and puts all
        properties into ``required`` on every object-level schema.
        """
        if not isinstance(schema, dict):
            return schema

        result = dict(schema)

        if result.get("type") == "object" and "properties" in result:
            result["additionalProperties"] = False
            result["required"] = list(result["properties"].keys())
            result["properties"] = {
                k: OpenAIProvider._make_strict_schema(v)
                for k, v in result["properties"].items()
            }

        if result.get("type") == "array" and "items" in result:
            result["items"] = OpenAIProvider._make_strict_schema(result["items"])

        return result

    def _build_tools(
        self, tools: list[dict] | None, *, strict: bool = True,
    ) -> list[dict] | None:
        if not tools:
            return None
        result = []
        for t in tools:
            params = t.get("parameters", t.get("input_schema", {}))
            func: dict = {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": self._make_strict_schema(params) if strict else params,
            }
            if strict:
                func["strict"] = True
            result.append({"type": "function", "function": func})
        return result

    @staticmethod
    def _map_tool_choice(tool_choice: str | dict | None) -> str | dict | None:
        """Map canonical tool_choice to OpenAI format."""
        if tool_choice is None:
            return None
        if isinstance(tool_choice, dict) and "name" in tool_choice:
            return {"type": "function", "function": {"name": tool_choice["name"]}}
        if tool_choice == "required":
            return "required"
        if tool_choice == "none":
            return "none"
        return "auto"

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
        api_messages = self._build_messages(messages, system_prompt)
        use_model = model or self.default_model

        kwargs: dict = {
            "model": use_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
        }
        api_tools = self._build_tools(tools)
        if api_tools:
            kwargs["tools"] = api_tools
        mapped_tc = self._map_tool_choice(tool_choice)
        if mapped_tc is not None and api_tools:
            kwargs["tool_choice"] = mapped_tc

        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        content = choice.message.content or ""
        tool_calls = []
        if choice.message.tool_calls:
            import json

            for tc in choice.message.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(
                        "Failed to parse tool call arguments for %s: %s",
                        tc.function.name, tc.function.arguments[:200] if tc.function.arguments else "None",
                    )
                    args = {"_raw_arguments": tc.function.arguments or ""}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": args,
                })

        tokens_in = response.usage.prompt_tokens if response.usage else 0
        tokens_out = response.usage.completion_tokens if response.usage else 0
        cost = self.estimate_cost(tokens_in, tokens_out, use_model)

        # Normalize stop_reason: OpenAI uses "tool_calls", Anthropic uses "tool_use"
        stop_reason = choice.finish_reason or ""
        if stop_reason == "tool_calls":
            stop_reason = "tool_use"

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            model=use_model,
            stop_reason=stop_reason,
            cost_usd=cost,
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
        api_messages = self._build_messages(messages, system_prompt)
        use_model = model or self.default_model

        kwargs: dict = {
            "model": use_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": api_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        api_tools = self._build_tools(tools)
        if api_tools:
            kwargs["tools"] = api_tools
        mapped_tc = self._map_tool_choice(tool_choice)
        if mapped_tc is not None and api_tools:
            kwargs["tool_choice"] = mapped_tc

        stream = await self.client.chat.completions.create(**kwargs)

        content_parts: list[str] = []
        tc_map: dict[int, dict] = {}  # index → {id, name, args}
        tokens_in = 0
        tokens_out = 0
        finish_reason = ""

        async for chunk in stream:
            if chunk.usage:
                tokens_in = chunk.usage.prompt_tokens or 0
                tokens_out = chunk.usage.completion_tokens or 0
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta.content:
                content_parts.append(delta.content)
                await on_token(delta.content)
            if delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tc_map:
                        tc_map[idx] = {"id": "", "name": "", "args": ""}
                    if tc_delta.id:
                        tc_map[idx]["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            tc_map[idx]["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            tc_map[idx]["args"] += tc_delta.function.arguments

        content = "".join(content_parts)
        tool_calls = []
        for idx in sorted(tc_map.keys()):
            tc = tc_map[idx]
            try:
                args = _json_mod.loads(tc["args"])
            except (_json_mod.JSONDecodeError, TypeError):
                logger.warning(
                    "Failed to parse streamed tool call arguments for %s: %s",
                    tc["name"], tc["args"][:200],
                )
                args = {"_raw_arguments": tc["args"]}
            tool_calls.append({"id": tc["id"], "name": tc["name"], "arguments": args})

        cost = self.estimate_cost(tokens_in, tokens_out, use_model)
        stop_reason = finish_reason
        if stop_reason == "tool_calls":
            stop_reason = "tool_use"

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            model=use_model,
            stop_reason=stop_reason,
            cost_usd=cost,
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
        tool_choice: str | dict | None = None,
    ) -> AsyncIterator[StreamChunk]:
        api_messages = self._build_messages(messages, system_prompt)

        stream = await self.client.chat.completions.create(
            model=model or self.default_model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=api_messages,
            stream=True,
        )
        async for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                yield StreamChunk(
                    delta=chunk.choices[0].delta.content,
                    chunk_type="text",
                )

    async def health_check(self) -> bool:
        try:
            return await self.health_check_detailed()
        except Exception:
            return False

    async def health_check_detailed(self) -> bool:
        response = await self.client.chat.completions.create(
            model=self.fast_model or self.default_model,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        return bool(response.choices)

    def estimate_cost(self, tokens_input: int, tokens_output: int, model: str) -> float:
        pricing = PRICING.get(model, {"input": 2.50, "output": 10.0})
        return (tokens_input * pricing["input"] + tokens_output * pricing["output"]) / 1_000_000

    async def list_models(self) -> list[dict]:
        known = [
            {"id": "gpt-4o", "name": "GPT-4o"},
            {"id": "gpt-4o-mini", "name": "GPT-4o Mini"},
            {"id": "o3", "name": "O3"},
            {"id": "o3-mini", "name": "O3 Mini"},
        ]
        return [
            {**m, "is_default": m["id"] == self.default_model}
            for m in known
        ]
