"""Self-hosted provider using OpenAI-compatible API.

Works with vLLM, Ollama, text-generation-inference, LM Studio, etc.
"""

import logging
import re
from collections.abc import AsyncIterator
from uuid import uuid4

import openai

from agents.providers.base import AIProvider
from agents.schemas.agent import LLMMessage, LLMResponse, StreamChunk

logger = logging.getLogger(__name__)


class SelfHostedProvider(AIProvider):
    provider_type = "self_hosted"

    def __init__(
        self,
        api_base_url: str,
        default_model: str,
        api_key: str | None = None,
        fast_model: str | None = None,
    ):
        self.client = openai.AsyncOpenAI(
            base_url=api_base_url,
            api_key=api_key or "not-needed",
        )
        self.default_model = default_model
        self.fast_model = fast_model

    @staticmethod
    def _parse_xml_tool_calls(content: str) -> list[dict]:
        """Parse XML-style tool calls from text content.

        Some self-hosted models (e.g. glm-latest) output tool calls as XML
        in the text content instead of using the native OpenAI tool_use API.

        Expected format:
        <tool_call>tool_name<arg_key>key</arg_key><arg_value>value</arg_value>...</tool_call>
        """
        results = []
        # Find all <tool_call>...</tool_call> blocks (content can span multiple lines)
        blocks = re.findall(r'<tool_call>(.*?)</tool_call>', content, flags=re.DOTALL)
        for block in blocks:
            block = block.strip()
            if not block:
                continue

            # Tool name is the text before the first <arg_key>
            name_match = re.match(r'^(.*?)<arg_key>', block, flags=re.DOTALL)
            if not name_match:
                # No arguments — entire block is the tool name (unlikely but handle it)
                tool_name = block.strip()
                if tool_name:
                    results.append({
                        "id": f"xmltc_{uuid4().hex[:8]}",
                        "name": tool_name,
                        "arguments": {},
                    })
                continue

            tool_name = name_match.group(1).strip()
            if not tool_name:
                continue

            # Extract arg_key/arg_value pairs
            # Use a pattern that matches each key then greedily captures up to
            # the next </arg_value> while being careful with nested content.
            arguments: dict[str, str] = {}
            pairs = re.findall(
                r'<arg_key>(.*?)</arg_key>\s*<arg_value>(.*?)</arg_value>',
                block,
                flags=re.DOTALL,
            )
            for key, value in pairs:
                arguments[key.strip()] = value

            results.append({
                "id": f"xmltc_{uuid4().hex[:8]}",
                "name": tool_name,
                "arguments": arguments,
            })

        return results

    def _build_messages(
        self, messages: list[LLMMessage], system_prompt: str | None = None
    ) -> list[dict]:
        import json as _json

        api_messages = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        for msg in messages:
            if msg.role == "assistant" and msg.tool_calls:
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
                for tr in msg.tool_results:
                    api_messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_use_id"],
                        "content": tr["content"],
                    })
            else:
                api_messages.append({"role": msg.role, "content": msg.content})
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

        logger.info("self_hosted: sending request model=%s msgs=%d tools=%d max_tokens=%d",
                     use_model, len(api_messages), len(api_tools) if api_tools else 0, max_tokens)

        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        content = choice.message.content or ""
        logger.info(
            "self_hosted: response finish_reason=%s content_len=%d native_tool_calls=%d",
            choice.finish_reason,
            len(content),
            len(choice.message.tool_calls) if choice.message.tool_calls else 0,
        )
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

        # Fallback: parse XML tool calls from content if model doesn't use native tool API
        xml_tool_calls_parsed = False
        if not tool_calls and content and "<tool_call>" in content:
            xml_calls = self._parse_xml_tool_calls(content)
            if xml_calls:
                logger.info(
                    "Parsed %d XML tool call(s) from text content (model: %s)",
                    len(xml_calls),
                    use_model,
                )
                tool_calls = xml_calls
                xml_tool_calls_parsed = True
                # Strip tool call XML from content to keep it clean
                content = re.sub(
                    r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL
                ).strip()

        tokens_in = response.usage.prompt_tokens if response.usage else 0
        tokens_out = response.usage.completion_tokens if response.usage else 0

        stop_reason = choice.finish_reason or ""
        if stop_reason == "tool_calls":
            stop_reason = "tool_use"
        if xml_tool_calls_parsed:
            stop_reason = "tool_use"

        logger.info(
            "self_hosted: final stop_reason=%s tool_calls=%d tokens_in=%d tokens_out=%d xml_parsed=%s",
            stop_reason, len(tool_calls), tokens_in, tokens_out, xml_tool_calls_parsed,
        )
        if tool_calls:
            for tc in tool_calls:
                logger.info("self_hosted: tool_call name=%s args_keys=%s", tc["name"], list(tc.get("arguments", {}).keys()))

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            model=use_model,
            stop_reason=stop_reason,
            cost_usd=0.0,  # self-hosted
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
        api_messages = []
        if system_prompt:
            api_messages.append({"role": "system", "content": system_prompt})
        for msg in messages:
            api_messages.append({"role": msg.role, "content": msg.content})

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
            model=self.default_model,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        return bool(response.choices)

    def estimate_cost(self, tokens_input: int, tokens_output: int, model: str) -> float:
        return 0.0  # self-hosted

    async def list_models(self) -> list[dict]:
        try:
            response = await self.client.models.list()
            models = []
            for m in response.data:
                models.append({
                    "id": m.id,
                    "name": m.id,
                    "is_default": m.id == self.default_model,
                })
            return sorted(models, key=lambda x: x["id"])
        except Exception:
            logger.warning("Failed to list models from self-hosted API, returning configured models")
            result = [{"id": self.default_model, "name": self.default_model, "is_default": True}]
            if self.fast_model:
                result.append({"id": self.fast_model, "name": self.fast_model, "is_default": False})
            return result
