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


# ── Hardened JSON repair ─────────────────────────────────────────────


def _fix_single_quotes(text: str) -> str:
    """Replace single-quoted strings with double-quoted, handling escapes."""
    result: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            # Skip over double-quoted string
            result.append(text[i])
            i += 1
            while i < len(text) and text[i] != '"':
                if text[i] == '\\' and i + 1 < len(text):
                    result.append(text[i])
                    i += 1
                    result.append(text[i])
                    i += 1
                    continue
                result.append(text[i])
                i += 1
            if i < len(text):
                result.append(text[i])
                i += 1
        elif text[i] == "'":
            # Convert single-quoted string to double-quoted
            result.append('"')
            i += 1
            while i < len(text) and text[i] != "'":
                if text[i] == '\\' and i + 1 < len(text):
                    result.append(text[i])
                    i += 1
                    result.append(text[i])
                    i += 1
                    continue
                if text[i] == '"':
                    result.append('\\"')
                    i += 1
                    continue
                result.append(text[i])
                i += 1
            result.append('"')
            if i < len(text):
                i += 1
        else:
            result.append(text[i])
            i += 1
    return "".join(result)


def _fix_control_chars_in_strings(text: str) -> str:
    """Escape unescaped newlines/tabs inside JSON strings."""
    result: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            result.append(ch)
            escape = False
            continue
        if ch == '\\':
            escape = True
            result.append(ch)
            continue
        if ch == '"':
            in_string = not in_string
            result.append(ch)
            continue
        if in_string:
            if ch == '\n':
                result.append('\\n')
                continue
            if ch == '\t':
                result.append('\\t')
                continue
            if ch == '\r':
                result.append('\\r')
                continue
        result.append(ch)
    return "".join(result)


def repair_json(text: str) -> str:
    """Attempt progressive repairs on malformed JSON strings.

    Applied in order of likelihood and safety:
    1. Fix trailing commas
    2. Strip JavaScript-style comments
    3. Replace Python booleans/None with JSON equivalents
    4. Replace single-quoted strings with double-quoted
    5. Fix unescaped control characters in strings
    """
    # 1. Trailing commas
    text = fix_trailing_commas(text)
    # 2. Strip // and /* */ comments (outside strings — best-effort)
    text = re.sub(r'//[^\n]*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    # 3. Python-isms (only outside strings — use word boundaries)
    text = re.sub(r'\bTrue\b', 'true', text)
    text = re.sub(r'\bFalse\b', 'false', text)
    text = re.sub(r'\bNone\b', 'null', text)
    # 4. Single quotes → double quotes
    text = _fix_single_quotes(text)
    # 5. Unescaped control chars
    text = _fix_control_chars_in_strings(text)
    return text


# ── Main parser ──────────────────────────────────────────────────────


def parse_llm_json(text: str) -> dict | None:
    """Extract and parse JSON from LLM output.

    Tries raw parse first, then applies progressive repairs.
    Returns parsed dict/list, or ``None`` on failure.
    """
    raw = extract_json(text)
    if raw is None:
        return None

    # Attempt 1: raw parse
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # Attempt 2: trailing comma fix only (fast, safe)
    try:
        return json.loads(fix_trailing_commas(raw))
    except json.JSONDecodeError:
        pass

    # Attempt 3: full repair chain
    try:
        return json.loads(repair_json(raw))
    except json.JSONDecodeError:
        pass

    # Attempt 4: re-extract from repaired text (handles nested issues)
    try:
        raw2 = extract_json(repair_json(text))
        if raw2:
            return json.loads(raw2)
    except (json.JSONDecodeError, Exception):
        pass

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
