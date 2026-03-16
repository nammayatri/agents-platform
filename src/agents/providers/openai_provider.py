from collections.abc import AsyncIterator

import openai

from agents.providers.base import AIProvider
from agents.schemas.agent import LLMMessage, LLMResponse, StreamChunk

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

    def _build_tools(self, tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", t.get("input_schema", {})),
                },
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

        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        content = choice.message.content or ""
        tool_calls = []
        if choice.message.tool_calls:
            import json

            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": json.loads(tc.function.arguments),
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
