"""Tool-based structured output for LLM phases.

Converts Pydantic schemas into a ``submit_result`` tool definition that the
LLM calls to produce structured output, replacing fragile text-based JSON
extraction.  Falls back to ``parse_llm_json`` when the tool is not called.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from agents.schemas.output import ROLE_OUTPUT_SCHEMAS
from agents.utils.json_helpers import parse_llm_json

logger = logging.getLogger(__name__)

SUBMIT_TOOL_NAME = "submit_result"


# ── Schema conversion ────────────────────────────────────────────────


def pydantic_to_json_schema(
    model_cls: type[BaseModel],
    *,
    exclude: set[str] | None = None,
) -> dict:
    """Convert a Pydantic model to a JSON Schema for tool parameters.

    Strips ``$defs``, ``title``, ``description`` top-level keys and removes
    *exclude* fields (e.g. ``raw_content`` which is injected by us).
    """
    exclude = exclude or set()
    schema = model_cls.model_json_schema()

    # Remove excluded fields
    if "properties" in schema:
        for field_name in exclude:
            schema["properties"].pop(field_name, None)
        if "required" in schema:
            schema["required"] = [r for r in schema["required"] if r not in exclude]

    # Strip meta keys that tool-calling APIs don't need
    for key in ("$defs", "definitions", "title", "description"):
        schema.pop(key, None)

    # Inline any $ref definitions (simple one-level)
    _inline_refs(schema, schema)

    return schema


def _inline_refs(node: Any, root: dict) -> Any:
    """Recursively inline ``$ref`` references in a JSON Schema."""
    if isinstance(node, dict):
        if "$ref" in node:
            ref_path = node["$ref"]  # e.g. "#/$defs/TestFailure"
            parts = ref_path.lstrip("#/").split("/")
            resolved = root
            for part in parts:
                resolved = resolved.get(part, {})  # type: ignore[union-attr]
            if isinstance(resolved, dict):
                result = dict(resolved)
                result.pop("title", None)
                return result
            return node
        return {k: _inline_refs(v, root) for k, v in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, root) for item in node]
    return node


# ── Tool builders ────────────────────────────────────────────────────


def build_submit_tool(
    schema: dict,
    name: str = "result",
    description: str = "",
) -> dict:
    """Build a ``submit_result`` tool definition from a JSON schema."""
    desc = description or (
        f"Submit your structured {name} output. "
        "Call this tool with the required fields when your work is complete."
    )
    return {
        "name": SUBMIT_TOOL_NAME,
        "description": desc,
        "input_schema": schema,
        "parameters": schema,  # OpenAI compat
    }


def build_submit_tool_for_role(role: str) -> dict | None:
    """Build a ``submit_result`` tool from a role's Pydantic output schema.

    Returns ``None`` for roles without a registered schema.
    """
    schema_cls = ROLE_OUTPUT_SCHEMAS.get(role)
    if schema_cls is None:
        return None

    schema = pydantic_to_json_schema(schema_cls, exclude={"raw_content"})
    return build_submit_tool(
        schema=schema,
        name=role,
        description=(
            f"Submit your structured output for the {role} role. "
            "Call this tool with the required fields when your work is complete."
        ),
    )


# ── Result extraction ────────────────────────────────────────────────


def extract_submit_result(
    tool_calls: list[dict] | None,
    content: str = "",
    *,
    messages: list | None = None,
) -> dict | None:
    """Extract the structured result from a tool loop execution.

    Priority:
    1. Check *tool_calls* from the final LLM response for ``submit_result``.
    2. Scan *messages* backwards for ``submit_result`` tool calls in history.
    3. Fall back to ``parse_llm_json`` on *content*.

    Returns the parsed dict or ``None``.
    """
    # 1. Direct tool calls on the final response
    if tool_calls:
        for tc in tool_calls:
            if tc.get("name") == SUBMIT_TOOL_NAME:
                args = tc.get("arguments", {})
                if isinstance(args, dict) and args:
                    # Skip sentinel dicts from unparseable args
                    if "_raw_arguments" in args and len(args) == 1:
                        logger.warning(
                            "submit_result tool call has unparseable _raw_arguments, skipping"
                        )
                        continue
                    logger.info("Extracted structured output via submit_result tool call")
                    return args
                logger.warning(
                    "submit_result tool call found but arguments empty or not a dict: %s",
                    type(args).__name__,
                )

    # 2. Scan message history backwards for submit_result
    if messages:
        for msg in reversed(messages):
            msg_tc = getattr(msg, "tool_calls", None) or []
            for tc in msg_tc:
                if tc.get("name") == SUBMIT_TOOL_NAME:
                    args = tc.get("arguments", {})
                    if isinstance(args, dict) and args:
                        if "_raw_arguments" in args and len(args) == 1:
                            continue
                        logger.info("Extracted structured output via submit_result in message history")
                        return args

    # 3. Fallback: parse from text content
    if content:
        result = parse_llm_json(content)
        if result is not None:
            logger.info("Extracted structured output via text fallback")
            return result

    return None
