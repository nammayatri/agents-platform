"""Shared JSON extraction and normalization utilities.

Centralizes LLM output parsing (markdown fences, brace matching)
and JSONB column normalization used across the codebase.
"""

import json
import re
from typing import Any


def extract_json(text: str) -> str | None:
    """Extract the first top-level JSON object from *text*.

    Handles:
    - Markdown code fences (``\\`json ... \\```)
    - Bare JSON mixed with prose
    - Strings containing braces (tracks in-string context)
    - Escape sequences inside strings

    Returns the raw JSON string, or ``None`` if no object found.
    """
    text = text.strip()

    # Strip markdown fence wrapper
    if text.startswith("```"):
        lines = text.split("\n")
        end = len(lines) - 1
        if lines[end].strip() == "```":
            text = "\n".join(lines[1:end])
        else:
            text = "\n".join(lines[1:])
        text = text.strip()

    # Find first balanced { ... } respecting quoted strings
    brace_start = text.find("{")
    if brace_start < 0:
        return None

    depth = 0
    in_string = False
    escape = False
    for i in range(brace_start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[brace_start : i + 1]

    return None


def fix_trailing_commas(json_str: str) -> str:
    """Remove trailing commas before ``}`` or ``]`` -- common LLM mistake."""
    return re.sub(r",\s*([}\]])", r"\1", json_str)


def parse_llm_json(text: str) -> dict | None:
    """Extract and parse JSON from LLM output.

    Tries raw parse first, then applies trailing comma fix.
    Returns parsed dict/list, or ``None`` on failure.
    """
    raw = extract_json(text)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(fix_trailing_commas(raw))
    except json.JSONDecodeError:
        return None


def safe_json(val: Any) -> dict:
    """Ensure a JSONB value from the database is a ``dict``.

    Handles ``None``, dicts, and double-encoded strings.
    """
    if val is None:
        return {}
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass
    return {}
