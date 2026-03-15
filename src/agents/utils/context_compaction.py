"""LLM-based context compaction for iteration history.

Compacts old iteration log entries into concise summaries using the fast model.
Reduces token usage while preserving actionable information (files modified,
learnings, errors, key decisions).

Compacted summaries are cached on entries (_compacted field) so each entry
is only compacted once — subsequent calls reuse the cached summary.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.providers.base import AIProvider

from agents.schemas.agent import LLMMessage

logger = logging.getLogger(__name__)

COMPACTION_PROMPT = """You are a technical summarizer. Summarize the following iteration log entries into concise summaries.

For each iteration, preserve:
1. What was attempted (subtask name and goal)
2. What files were modified (list paths)
3. What succeeded or failed (with specific reasons)
4. Key learnings (actionable insights for future iterations)
5. Any errors encountered (with error types/messages)

Output a JSON array of objects, one per iteration:
[
  {
    "iteration": <number>,
    "subtask": "<name>",
    "summary": "<1-2 sentence summary of what happened>",
    "files_modified": ["<path1>", "<path2>"],
    "status": "passed" | "failed",
    "learnings": ["<learning1>", "<learning2>"],
    "errors": "<error summary or null>"
  }
]

Be concise but don't lose actionable information. Each summary should be 1-2 sentences maximum.

Here are the iteration entries to summarize:
"""


async def compact_iteration_log(
    entries: list[dict],
    provider: "AIProvider",
    keep_recent: int = 3,
) -> list[dict]:
    """Compact old iteration log entries using the fast LLM model.

    Keeps the last `keep_recent` entries in full detail. For older entries,
    uses the fast model to generate concise summaries. Caches compacted
    summaries on the entry dict (`_compacted` field) so each entry is
    compacted only once.

    Args:
        entries: List of iteration log entry dicts.
        provider: AI provider instance (uses fast model for compaction).
        keep_recent: Number of recent entries to keep in full detail.

    Returns:
        Mixed list: compacted older entries + full recent entries.
    """
    if len(entries) <= keep_recent:
        return entries

    old_entries = entries[:-keep_recent]
    recent_entries = entries[-keep_recent:]

    # Separate already-compacted from needing-compaction
    needs_compaction = []
    already_compacted = []

    for entry in old_entries:
        if "_compacted" in entry:
            already_compacted.append(entry)
        else:
            needs_compaction.append(entry)

    if not needs_compaction:
        # All old entries are already compacted
        return old_entries + recent_entries

    # Compact the entries that need it
    try:
        compacted = await _compact_entries(needs_compaction, provider)

        # Cache the compacted summaries back onto the original entries
        for entry, summary in zip(needs_compaction, compacted):
            entry["_compacted"] = summary

        logger.info(
            "context_compaction: compacted %d entries (kept %d recent)",
            len(needs_compaction), keep_recent,
        )
    except Exception as e:
        logger.warning("context_compaction: LLM compaction failed: %s, using fallback", e)
        # Fallback: use heuristic compaction
        for entry in needs_compaction:
            entry["_compacted"] = _heuristic_compact(entry)

    return old_entries + recent_entries


async def _compact_entries(entries: list[dict], provider: "AIProvider") -> list[dict]:
    """Use the fast model to generate summaries for iteration entries."""
    # Prepare the entries for the LLM (strip large fields)
    clean_entries = []
    for entry in entries:
        clean = {
            "iteration": entry.get("iteration", "?"),
            "subtask": entry.get("subtask_title", entry.get("subtask", "?")),
            "status": "passed" if entry.get("qc_passed") else "failed",
            "files_modified": entry.get("files_modified", []),
            "learnings": entry.get("learnings", []),
            "error": entry.get("error"),
            "output_preview": (entry.get("output", "") or "")[:500],
        }
        clean_entries.append(clean)

    prompt_content = COMPACTION_PROMPT + json.dumps(clean_entries, indent=2)

    messages = [LLMMessage(role="user", content=prompt_content)]

    response = await provider.send_message(
        messages,
        model=provider.get_model(use_fast=True),
        max_tokens=2000,
        temperature=0.0,
    )

    # Parse the JSON response
    content = response.content or ""

    # Try to extract JSON from the response
    summaries = _extract_json_array(content)

    if not summaries or len(summaries) != len(entries):
        logger.warning(
            "context_compaction: LLM returned %d summaries for %d entries, using fallback",
            len(summaries) if summaries else 0, len(entries),
        )
        return [_heuristic_compact(e) for e in entries]

    return summaries


def _extract_json_array(text: str) -> list[dict] | None:
    """Extract a JSON array from text that may contain markdown fences."""
    import re

    # Try direct parse
    text = text.strip()

    # Remove markdown fences
    text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'^```\s*$', '', text, flags=re.MULTILINE)
    text = text.strip()

    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find array in text
    match = re.search(r'\[[\s\S]*\]', text)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None


def _heuristic_compact(entry: dict) -> dict:
    """Fallback heuristic compaction when LLM is unavailable."""
    return {
        "iteration": entry.get("iteration", "?"),
        "subtask": entry.get("subtask_title", entry.get("subtask", "?")),
        "summary": f"{'Passed' if entry.get('qc_passed') else 'Failed'}: {entry.get('subtask_title', 'unknown subtask')}",
        "files_modified": entry.get("files_modified", [])[:10],
        "status": "passed" if entry.get("qc_passed") else "failed",
        "learnings": entry.get("learnings", [])[:5],
        "errors": str(entry.get("error", ""))[:200] if entry.get("error") else None,
    }


def format_compacted_entry(entry: dict) -> str:
    """Format a compacted entry for inclusion in context.

    Works with both LLM-compacted and heuristic-compacted entries.
    """
    compacted = entry.get("_compacted", entry)

    lines = [
        f"Iteration {compacted.get('iteration', '?')} (subtask: {compacted.get('subtask', '?')}):",
        f"  Status: {compacted.get('status', 'unknown')}",
        f"  Summary: {compacted.get('summary', 'N/A')}",
    ]

    files = compacted.get("files_modified", [])
    if files:
        lines.append(f"  Files: {', '.join(files[:10])}")

    learnings = compacted.get("learnings", [])
    if learnings:
        for l in learnings[:5]:
            lines.append(f"  - {l}")

    errors = compacted.get("errors")
    if errors:
        lines.append(f"  Error: {errors}")

    return "\n".join(lines)
