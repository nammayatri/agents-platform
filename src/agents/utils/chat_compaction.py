"""4-tier progressive context compaction for chat conversations.

Inspired by Claude Code's compaction architecture. Instead of dumb truncation
that loses critical user instructions, this pipeline applies increasingly
aggressive strategies to reclaim context space while preserving what matters.

Tier 1: Tool result clearing — replace verbose tool outputs in old messages
        with short placeholders (zero LLM cost, biggest impact)
Tier 2: LLM summarization — compress older conversation into a structured
        summary that preserves decisions, constraints, and file context
Tier 3: Session memory — persist the summary to DB so it survives across
        reconnects and can be re-injected after further compaction
Tier 4: Hard truncation — last resort fallback, drop oldest messages

Usage:
    messages = compact_chat_context(
        messages, max_tokens=40_000, model="claude-...",
        provider=provider,  # for Tier 2
        db=db, session_id=session_id,  # for Tier 3
    )
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agents.providers.base import AIProvider

from agents.schemas.agent import LLMMessage
from agents.utils.token_counter import count_tokens

logger = logging.getLogger(__name__)

# Messages newer than this are never compacted (3 exchange pairs)
KEEP_RECENT = 6

# Patterns that indicate large tool/code output in message content
_TOOL_OUTPUT_PATTERNS = [
    # Code blocks with file contents (```lang ... ```)
    (re.compile(r'```[\w]*\n.{500,}?\n```', re.DOTALL), '[Code block cleared — {n} chars]'),
    # Raw command output blocks
    (re.compile(r'(?:^|\n)(?:stdout|stderr|Output|Result):\s*\n.{300,}?(?=\n\n|\Z)', re.DOTALL),
     '[Command output cleared — {n} chars]'),
    # JSON dumps
    (re.compile(r'```json\n\{[\s\S]{500,}?\}\n```', re.DOTALL), '[JSON output cleared — {n} chars]'),
    # File tree dumps
    (re.compile(r'(?:Directory tree|File tree|├──|└──)[\s\S]{300,}?(?=\n\n|\Z)'),
     '[File tree cleared — {n} chars]'),
]

# Fields in metadata_json that contain large payloads
_LARGE_METADATA_KEYS = ['raw_output', 'execution', 'plan_data']


# ═══════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════


async def compact_chat_context(
    messages: list[dict],
    max_tokens: int,
    model: str,
    *,
    provider: AIProvider | None = None,
    db=None,
    session_id: str | None = None,
    existing_summary: str | None = None,
) -> list[dict]:
    """Progressive compaction pipeline for chat messages.

    Applies tiers in order until context fits within max_tokens.
    Each tier is more aggressive but also more lossy.

    Parameters
    ----------
    messages : list[dict]
        Chat messages with 'role' and 'content' keys.
    max_tokens : int
        Target token budget for the conversation.
    model : str
        Model name for token counting.
    provider : AIProvider, optional
        Required for Tier 2 (LLM summarization).
    db : asyncpg.Pool, optional
        Required for Tier 3 (session memory persistence).
    session_id : str, optional
        Required for Tier 3.
    existing_summary : str, optional
        Previously persisted compaction summary to prepend.

    Returns
    -------
    list[dict]
        Compacted messages that fit within the token budget.
    """
    if not messages:
        return messages

    total = _count_messages_tokens(messages, model)
    if total <= max_tokens:
        # Prepend existing summary if we have one and there's room
        if existing_summary:
            return [{"role": "system", "content": existing_summary}] + list(messages)
        return list(messages)

    logger.info(
        "chat_compaction: %d tokens exceeds %d budget, starting pipeline (%d messages)",
        total, max_tokens, len(messages),
    )

    # ── Tier 1: Clear old tool results (zero cost) ──────────────
    messages = _tier1_clear_tool_results(messages, model)
    total = _count_messages_tokens(messages, model)
    if total <= max_tokens:
        logger.info("chat_compaction: Tier 1 sufficient (%d tokens)", total)
        if existing_summary:
            return [{"role": "system", "content": existing_summary}] + messages
        return messages

    # ── Tier 2: LLM summarization ───────────────────────────────
    if provider:
        try:
            messages = await _tier2_llm_summarize(messages, max_tokens, model, provider)
            total = _count_messages_tokens(messages, model)
            if total <= max_tokens:
                logger.info("chat_compaction: Tier 2 sufficient (%d tokens)", total)

                # ── Tier 3: Persist summary to session ──────────
                if db and session_id:
                    await _tier3_persist_summary(messages, db, session_id)

                return messages
        except Exception:
            logger.warning("chat_compaction: Tier 2 failed, falling through", exc_info=True)

    # ── Tier 4: Hard truncation (last resort) ───────────────────
    messages = _tier4_truncate(messages, max_tokens, model)
    total = _count_messages_tokens(messages, model)
    logger.info("chat_compaction: Tier 4 fallback (%d tokens, %d messages)", total, len(messages))
    return messages


# ═══════════════════════════════════════════════════════════════════
# Tier 1: Clear old tool results
# ═══════════════════════════════════════════════════════════════════


def _tier1_clear_tool_results(messages: list[dict], model: str) -> list[dict]:
    """Replace verbose tool outputs in older messages with short placeholders.

    Only touches messages outside the KEEP_RECENT window.
    Preserves message structure (role, id, etc.) — only modifies content.
    This is the highest-value, lowest-cost compaction step.
    """
    if len(messages) <= KEEP_RECENT:
        return list(messages)

    older = messages[:-KEEP_RECENT]
    recent = messages[-KEEP_RECENT:]

    cleared = []
    total_saved = 0
    for msg in older:
        content = msg.get("content") or ""
        original_len = len(content)
        new_content = _clear_verbose_content(content)
        saved = original_len - len(new_content)
        total_saved += saved
        cleared.append({**msg, "content": new_content})

    if total_saved > 0:
        logger.info(
            "chat_compaction tier1: cleared %d chars from %d older messages",
            total_saved, len(older),
        )

    return cleared + list(recent)


def _clear_verbose_content(content: str) -> str:
    """Strip large code blocks, command outputs, and JSON dumps from content."""
    for pattern, placeholder_tpl in _TOOL_OUTPUT_PATTERNS:
        def _replacer(m: re.Match) -> str:
            return placeholder_tpl.format(n=len(m.group()))
        content = pattern.sub(_replacer, content)
    return content


# ═══════════════════════════════════════════════════════════════════
# Tier 2: LLM summarization
# ═══════════════════════════════════════════════════════════════════

_SUMMARIZE_PROMPT = """\
You are summarizing a conversation to preserve context for continuation.

Create a structured summary that preserves ALL of the following:

1. **User requests & constraints** — Every explicit instruction, preference, or constraint \
the user stated (e.g., "always use TypeScript", "don't modify the auth module"). These are \
the MOST important things to preserve.

2. **What was accomplished** — Completed tasks, files created/modified, PRs created, \
configs changed. Include specific file paths.

3. **Current work in progress** — What the assistant was working on when this summary \
was generated. Include enough detail to resume seamlessly.

4. **Key decisions** — Architectural choices, trade-offs discussed, approaches selected \
or rejected and why.

5. **Active plan / next steps** — Any planned work not yet started.

6. **Important code context** — Key file paths, function names, data structures that \
are central to the ongoing work.

Format as a structured summary with clear sections. Be concise but DO NOT drop any \
user constraints or decisions — those are critical for continuity.

Conversation to summarize:
"""


async def _tier2_llm_summarize(
    messages: list[dict],
    max_tokens: int,
    model: str,
    provider: AIProvider,
) -> list[dict]:
    """Summarize older messages using the fast LLM model.

    Keeps KEEP_RECENT messages intact. Replaces everything before them
    with a single summary message.
    """
    if len(messages) <= KEEP_RECENT:
        return list(messages)

    older = messages[:-KEEP_RECENT]
    recent = messages[-KEEP_RECENT:]

    # Build conversation text for summarization
    conv_lines = []
    for msg in older:
        role = msg.get("role", "unknown").upper()
        content = (msg.get("content") or "")[:3000]  # cap per message
        conv_lines.append(f"[{role}]: {content}")

    conv_text = "\n\n".join(conv_lines)

    # Cap total input to summarizer to avoid blowing its context
    if len(conv_text) > 30000:
        conv_text = conv_text[:30000] + "\n\n[... earlier messages truncated ...]"

    summary_messages = [
        LLMMessage(role="user", content=_SUMMARIZE_PROMPT + conv_text),
    ]

    response = await provider.send_message(
        summary_messages,
        model=provider.get_model(use_fast=True),
        max_tokens=2000,
        temperature=0.0,
    )

    summary = (response.content or "").strip()
    if not summary or len(summary) < 50:
        raise ValueError("LLM returned empty or too-short summary")

    # Build the compacted message list:
    # [summary as system message] + [recent messages]
    summary_msg = {
        "role": "system",
        "content": (
            f"[Conversation summary — {len(older)} earlier messages compacted]\n\n"
            f"{summary}"
        ),
    }

    result = [summary_msg] + list(recent)
    logger.info(
        "chat_compaction tier2: summarized %d messages into %d-char summary, kept %d recent",
        len(older), len(summary), len(recent),
    )
    return result


# ═══════════════════════════════════════════════════════════════════
# Tier 3: Persist summary to session
# ═══════════════════════════════════════════════════════════════════


async def _tier3_persist_summary(messages: list[dict], db, session_id: str) -> None:
    """Persist the compaction summary to the session for future recovery.

    Stores in project_chat_sessions.compaction_summary so it can be
    re-injected on session reload without re-running the LLM.
    """
    # Find the summary message (the first system message with our marker)
    summary_text = None
    for msg in messages:
        if msg.get("role") == "system" and "[Conversation summary" in (msg.get("content") or ""):
            summary_text = msg["content"]
            break

    if not summary_text:
        return

    try:
        await db.execute(
            "UPDATE project_chat_sessions SET compaction_summary = $2, updated_at = NOW() "
            "WHERE id = $1",
            session_id, summary_text,
        )
        logger.info("chat_compaction tier3: persisted summary to session %s", session_id[:8])
    except Exception:
        logger.debug("chat_compaction tier3: failed to persist summary", exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# Tier 4: Hard truncation (last resort)
# ═══════════════════════════════════════════════════════════════════


def _tier4_truncate(messages: list[dict], max_tokens: int, model: str) -> list[dict]:
    """Drop oldest messages until we fit within budget.

    Always keeps at least 2 messages (the most recent exchange).
    """
    result = list(messages)

    while len(result) > 2:
        total = _count_messages_tokens(result, model)
        if total <= max_tokens:
            break
        # Drop the oldest message
        dropped = result.pop(0)
        logger.debug(
            "chat_compaction tier4: dropped %s message (%d chars)",
            dropped.get("role"), len(dropped.get("content") or ""),
        )

    return result


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════


def _count_messages_tokens(messages: list[dict], model: str) -> int:
    """Count total tokens across all messages."""
    return sum(count_tokens(m.get("content") or "", model) for m in messages)
