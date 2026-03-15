"""Extract persistent memories from completed tasks.

After a task completes, this module uses the LLM to extract structured
learnings (architecture decisions, patterns, conventions, pitfalls)
that can be injected into future task contexts.

Includes deduplication via difflib.SequenceMatcher to avoid storing
redundant memories.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.providers.base import AIProvider

from agents.schemas.agent import LLMMessage

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Analyze the following completed task and extract reusable learnings for future tasks in this project.

Extract 3-8 structured memories. Focus on:
1. **Architecture decisions**: How components are structured, important patterns used
2. **Code patterns**: Recurring patterns, preferred approaches, coding conventions
3. **Conventions**: Naming conventions, file organization, import patterns
4. **Pitfalls**: Things that went wrong, common mistakes to avoid
5. **Dependencies**: Key dependencies, their usage patterns, version constraints

For each memory, provide:
- category: one of "architecture", "pattern", "convention", "pitfall", "dependency"
- content: a clear, actionable statement (1-2 sentences)
- confidence: 0.0 to 1.0 (how confident this is a reusable learning vs task-specific)

Output a JSON array:
[
  {{"category": "pattern", "content": "The project uses repository pattern for data access...", "confidence": 0.9}},
  ...
]

Task title: {title}
Task summary: {summary}

Iteration history:
{iteration_log}
"""


@dataclass
class Memory:
    """A single extracted memory."""
    category: str
    content: str
    confidence: float


async def extract_memories(
    iteration_log: list[dict],
    task_title: str,
    task_summary: str,
    provider: "AIProvider",
) -> list[Memory]:
    """Extract reusable memories from a completed task.

    Uses the LLM to analyze the task's iteration history and extract
    structured learnings. Uses the fast model to minimize cost.
    """
    # Build condensed iteration log for the LLM
    log_text = _format_iteration_log(iteration_log)

    prompt = EXTRACTION_PROMPT.format(
        title=task_title,
        summary=task_summary or "No summary available",
        iteration_log=log_text,
    )

    messages = [LLMMessage(role="user", content=prompt)]

    try:
        response = await provider.send_message(
            messages,
            model=provider.get_model(use_fast=True),
            max_tokens=2000,
            temperature=0.1,
        )
    except Exception as e:
        logger.warning("memory_extractor: LLM call failed: %s", e)
        return []

    content = response.content or ""
    memories = _parse_memories(content)

    logger.info("memory_extractor: extracted %d memories from task '%s'", len(memories), task_title)
    return memories


async def deduplicate_memories(
    new_memories: list[Memory],
    existing_contents: list[str],
    similarity_threshold: float = 0.8,
) -> list[Memory]:
    """Filter out memories that are too similar to existing ones.

    Uses difflib.SequenceMatcher for text similarity comparison.
    """
    unique = []
    for memory in new_memories:
        is_duplicate = False
        for existing in existing_contents:
            similarity = difflib.SequenceMatcher(
                None, memory.content.lower(), existing.lower()
            ).ratio()
            if similarity > similarity_threshold:
                logger.debug(
                    "memory_extractor: skipping duplicate (%.2f similarity): %s",
                    similarity, memory.content[:80],
                )
                is_duplicate = True
                break
        if not is_duplicate:
            unique.append(memory)

    return unique


def _format_iteration_log(entries: list[dict]) -> str:
    """Format iteration log entries for the extraction prompt."""
    lines = []
    for entry in entries[-10:]:  # Last 10 iterations
        lines.append(f"Iteration {entry.get('iteration', '?')}:")
        lines.append(f"  Subtask: {entry.get('subtask_title', '?')}")
        lines.append(f"  Status: {'passed' if entry.get('qc_passed') else 'failed'}")

        files = entry.get("files_modified", [])
        if files:
            lines.append(f"  Files: {', '.join(files[:5])}")

        learnings = entry.get("learnings", [])
        if learnings:
            for l in learnings[:3]:
                lines.append(f"  Learning: {l}")

        error = entry.get("error")
        if error:
            lines.append(f"  Error: {str(error)[:200]}")

        lines.append("")

    return "\n".join(lines)


def _parse_memories(content: str) -> list[Memory]:
    """Parse LLM response into Memory objects."""
    # Remove markdown fences
    content = content.strip()
    content = re.sub(r'^```(?:json)?\s*', '', content, flags=re.MULTILINE)
    content = re.sub(r'^```\s*$', '', content, flags=re.MULTILINE)
    content = content.strip()

    # Try to parse JSON array
    try:
        data = json.loads(content)
        if isinstance(data, list):
            return _validate_memories(data)
    except json.JSONDecodeError:
        pass

    # Try to find array in text
    match = re.search(r'\[[\s\S]*\]', content)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return _validate_memories(data)
        except json.JSONDecodeError:
            pass

    logger.warning("memory_extractor: failed to parse LLM response as JSON")
    return []


VALID_CATEGORIES = {"architecture", "pattern", "convention", "pitfall", "dependency"}


def _validate_memories(data: list) -> list[Memory]:
    """Validate and convert raw dicts to Memory objects."""
    memories = []
    for item in data:
        if not isinstance(item, dict):
            continue

        category = item.get("category", "").lower()
        content = item.get("content", "")
        confidence = item.get("confidence", 0.5)

        if not category or not content:
            continue

        # Normalize category
        if category not in VALID_CATEGORIES:
            category = "pattern"  # default

        # Clamp confidence
        confidence = max(0.0, min(1.0, float(confidence)))

        memories.append(Memory(
            category=category,
            content=content,
            confidence=confidence,
        ))

    return memories[:8]  # Cap at 8 memories per task
