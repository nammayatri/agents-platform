"""Structured output validation for agent responses.

Extracts JSON from LLM text, validates against the role's Pydantic schema,
and produces correction prompts for retry on failure.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal, get_args, get_origin

from pydantic import ValidationError

from agents.schemas.output import ROLE_OUTPUT_SCHEMAS, BaseAgentOutput
from agents.utils.json_helpers import extract_json, fix_trailing_commas

logger = logging.getLogger(__name__)

MAX_VALIDATION_RETRIES = 2


# Re-export for backward compatibility
extract_json_from_content = extract_json


# ── Core validation ─────────────────────────────────────────────────


def validate_agent_output(
    role: str,
    raw_content: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate LLM output against the role's Pydantic schema.

    Returns ``(validated_dict, errors)``.

    * *validated_dict*: ``.model_dump()`` when valid, ``None`` when invalid.
    * *errors*: list of human-readable error strings (empty when valid).

    For roles **not** in ``ROLE_OUTPUT_SCHEMAS`` the raw content is returned
    as-is with no errors (backward-compatible passthrough).
    """
    schema_cls = ROLE_OUTPUT_SCHEMAS.get(role)
    if schema_cls is None:
        return {"content": raw_content, "raw_content": raw_content}, []

    # 1. Extract JSON from LLM text
    json_str = extract_json_from_content(raw_content)
    if json_str is None:
        return None, [
            "No JSON found. Include a JSON object with fields: "
            f"{_describe_required_fields(schema_cls)}"
        ]

    # 2. Parse JSON (fix trailing commas — common LLM mistake)
    try:
        fixed = fix_trailing_commas(json_str)
        data = json.loads(fixed)
    except json.JSONDecodeError as exc:
        return None, [f"Malformed JSON: {exc}"]

    # 3. Inject raw_content (agent doesn't produce this itself)
    data["raw_content"] = raw_content

    # 4. Pydantic validation
    try:
        validated = schema_cls.model_validate(data)
        result = validated.model_dump()
        # Backward compat: downstream code reads output_result["content"]
        result["content"] = raw_content
        return result, []
    except ValidationError as exc:
        errors: list[str] = []
        for err in exc.errors():
            field_path = " -> ".join(str(loc) for loc in err["loc"])
            errors.append(f"'{field_path}': {err['msg']}")
        return None, errors


def validate_agent_output_dict(
    role: str,
    data: dict[str, Any],
    raw_content: str = "",
) -> tuple[dict[str, Any] | None, list[str]]:
    """Validate a pre-parsed dict (from tool call arguments) against the schema.

    Use this when structured output comes from a ``submit_result`` tool call
    rather than text extraction.
    """
    schema_cls = ROLE_OUTPUT_SCHEMAS.get(role)
    if schema_cls is None:
        data.setdefault("raw_content", raw_content)
        data.setdefault("content", raw_content)
        return data, []

    data["raw_content"] = raw_content

    try:
        validated = schema_cls.model_validate(data)
        result = validated.model_dump()
        result["content"] = raw_content
        return result, []
    except ValidationError as exc:
        errors: list[str] = []
        for err in exc.errors():
            field_path = " -> ".join(str(loc) for loc in err["loc"])
            errors.append(f"'{field_path}': {err['msg']}")
        return None, errors


# ── Correction prompt ───────────────────────────────────────────────


def build_correction_prompt(
    role: str,
    errors: list[str],
    original_response: str,  # noqa: ARG001  kept for potential future use
) -> str:
    """Build a concise follow-up telling the agent what to fix."""
    schema_cls = ROLE_OUTPUT_SCHEMAS.get(role)
    example = _build_example_json(schema_cls) if schema_cls else "{}"

    error_list = "\n".join(f"- {e}" for e in errors)
    return (
        f"Output validation failed:\n{error_list}\n\n"
        f"Required JSON format:\n```json\n{example}\n```\n"
        "Fix the errors and include the JSON in your response."
    )


# ── Prompt instruction ──────────────────────────────────────────────


def build_structured_output_instruction(role: str) -> str:
    """Generate a compact prompt block specifying the required JSON output.

    Returns an empty string for roles without a registered schema.
    """
    schema_cls = ROLE_OUTPUT_SCHEMAS.get(role)
    if schema_cls is None:
        return ""

    example = _build_example_json(schema_cls)

    return (
        "\n\n## Output Format\n"
        f"Include this JSON in your response:\n```json\n{example}\n```"
    )


# ── Internal helpers ────────────────────────────────────────────────


def _describe_required_fields(schema_cls: type[BaseAgentOutput]) -> str:
    fields = []
    for name, info in schema_cls.model_fields.items():
        if name == "raw_content":
            continue
        if info.is_required():
            fields.append(name)
    return ", ".join(fields) if fields else "(see schema)"


def _get_literal_values(annotation: Any) -> list[str] | None:
    """Extract allowed values from a Literal type annotation."""
    if get_origin(annotation) is Literal:
        return list(get_args(annotation))
    return None


def _build_example_json(schema_cls: type[BaseAgentOutput] | None) -> str:
    if schema_cls is None:
        return "{}"
    example: dict[str, Any] = {}
    for name, info in schema_cls.model_fields.items():
        if name == "raw_content":
            continue
        ann = info.annotation
        # Check for Literal types first — show allowed values
        literal_vals = _get_literal_values(ann)
        if literal_vals:
            example[name] = "|".join(str(v) for v in literal_vals)
        elif ann is str or ann == str:
            example[name] = "..."
        elif ann is bool or ann == bool:
            example[name] = True
        elif hasattr(ann, "__origin__") and getattr(ann, "__origin__", None) is list:
            # Check inner type for Literal
            inner_args = get_args(ann)
            if inner_args:
                inner = inner_args[0]
                if hasattr(inner, "model_fields"):
                    # Nested Pydantic model — show field names
                    nested = {}
                    for fname, finfo in inner.model_fields.items():
                        fann = finfo.annotation
                        flit = _get_literal_values(fann)
                        if flit:
                            nested[fname] = "|".join(str(v) for v in flit)
                        elif fann is str or fann == str:
                            nested[fname] = "..."
                        elif fann is bool or fann == bool:
                            nested[fname] = True
                        else:
                            nested[fname] = "..."
                    example[name] = [nested]
                else:
                    example[name] = ["..."]
            else:
                example[name] = ["..."]
        elif ann is list:
            example[name] = ["..."]
        else:
            example[name] = "..."
    return json.dumps(example, indent=2)
