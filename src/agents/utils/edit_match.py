"""Progressive fuzzy edit matching for the edit_file tool.

4-layer matching strategy:
  Layer 1: Exact match (current behavior)
  Layer 2: Whitespace-normalized match (collapse whitespace runs, strip trailing)
  Layer 3: Fuzzy line-by-line match (difflib.SequenceMatcher, threshold configurable)
  Layer 4: Indentation-agnostic match (strip leading whitespace, match content, re-apply indent)
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """Result of a successful match."""
    start_line: int      # 0-indexed line number where match starts
    end_line: int        # 0-indexed line number where match ends (exclusive)
    matched_text: str    # The actual text that was matched in the file
    confidence: float    # 0.0 to 1.0 — how confident we are in the match
    method: str          # Which matching layer found the match


class EditMatchError(Exception):
    """Raised when no acceptable match can be found."""
    def __init__(self, message: str, best_partial: MatchResult | None = None):
        super().__init__(message)
        self.best_partial = best_partial


def find_best_match(
    content: str,
    search_block: str,
    min_confidence: float = 0.85,
) -> MatchResult | None:
    """Find the best match for search_block in content using progressive matching.

    Tries each layer in order, returning the first match that meets min_confidence.
    Returns None if no match meets the threshold.
    """
    if not search_block or not content:
        return None

    # Layer 1: Exact match
    result = _exact_match(content, search_block)
    if result:
        logger.debug("edit_match: Layer 1 (exact) matched at line %d", result.start_line)
        return result

    # Layer 2: Whitespace-normalized
    result = _whitespace_match(content, search_block)
    if result and result.confidence >= min_confidence:
        logger.debug("edit_match: Layer 2 (whitespace) matched at line %d (confidence=%.3f)",
                     result.start_line, result.confidence)
        return result

    # Layer 3: Fuzzy line-by-line
    result = _fuzzy_match(content, search_block, min_confidence)
    if result and result.confidence >= min_confidence:
        logger.debug("edit_match: Layer 3 (fuzzy) matched at line %d (confidence=%.3f)",
                     result.start_line, result.confidence)
        return result

    # Layer 4: Indentation-agnostic
    result = _indent_agnostic_match(content, search_block, min_confidence)
    if result and result.confidence >= min_confidence:
        logger.debug("edit_match: Layer 4 (indent-agnostic) matched at line %d (confidence=%.3f)",
                     result.start_line, result.confidence)
        return result

    logger.debug("edit_match: No match found above threshold %.2f", min_confidence)
    return None


def apply_edit(
    content: str,
    search_block: str,
    replace_block: str,
    min_confidence: float = 0.85,
) -> tuple[str, MatchResult]:
    """Apply an edit by finding search_block in content and replacing with replace_block.

    Returns (new_content, match_result).
    Raises EditMatchError if no acceptable match is found.
    """
    match = find_best_match(content, search_block, min_confidence)

    if match is None:
        # Try to find best partial match for error context
        partial = _fuzzy_match(content, search_block, min_confidence=0.0)

        # Build helpful error message
        error_msg = f"Could not find a match for the search block (min_confidence={min_confidence})."
        if partial and partial.confidence > 0.3:
            # Show the nearby content to help the agent understand what's there
            content_lines = content.split("\n")
            ctx_start = max(0, partial.start_line - 2)
            ctx_end = min(len(content_lines), partial.end_line + 2)
            context_snippet = "\n".join(content_lines[ctx_start:ctx_end])
            error_msg += (
                f"\nBest partial match (confidence={partial.confidence:.2f}) at lines "
                f"{partial.start_line + 1}-{partial.end_line}:\n"
                f"---\n{context_snippet}\n---"
            )

        raise EditMatchError(error_msg, best_partial=partial)

    # Apply the replacement
    content_lines = content.split("\n")
    search_lines = search_block.split("\n")
    replace_lines = replace_block.split("\n")

    if match.method == "indent_agnostic":
        # Re-apply the original indentation to the replacement
        replace_lines = _reindent_replacement(
            content_lines[match.start_line:match.end_line],
            search_lines,
            replace_lines,
        )

    new_lines = content_lines[:match.start_line] + replace_lines + content_lines[match.end_line:]
    return "\n".join(new_lines), match


# ── Layer 1: Exact Match ────────────────────────────────────────

def _exact_match(content: str, search_block: str) -> MatchResult | None:
    """Layer 1: Simple exact substring match."""
    idx = content.find(search_block)
    if idx == -1:
        return None

    start_line = content[:idx].count("\n")
    end_line = start_line + search_block.count("\n") + 1

    return MatchResult(
        start_line=start_line,
        end_line=end_line,
        matched_text=search_block,
        confidence=1.0,
        method="exact",
    )


# ── Layer 2: Whitespace-Normalized Match ────────────────────────

def _normalize_whitespace(text: str) -> str:
    """Collapse whitespace runs, strip trailing whitespace per line."""
    lines = text.split("\n")
    normalized = []
    for line in lines:
        # Strip trailing whitespace
        line = line.rstrip()
        # Collapse runs of spaces/tabs within the line (but preserve leading indent structure)
        # Only collapse internal whitespace, not leading
        stripped = line.lstrip()
        if stripped:
            indent = line[:len(line) - len(stripped)]
            stripped = re.sub(r'[ \t]+', ' ', stripped)
            normalized.append(indent + stripped)
        else:
            normalized.append("")
    return "\n".join(normalized)


def _whitespace_match(content: str, search_block: str) -> MatchResult | None:
    """Layer 2: Match after normalizing whitespace."""
    norm_content = _normalize_whitespace(content)
    norm_search = _normalize_whitespace(search_block)

    idx = norm_content.find(norm_search)
    if idx == -1:
        return None

    # Map back to original line numbers
    start_line = norm_content[:idx].count("\n")
    search_line_count = norm_search.count("\n") + 1
    end_line = start_line + search_line_count

    content_lines = content.split("\n")
    matched = "\n".join(content_lines[start_line:end_line])

    return MatchResult(
        start_line=start_line,
        end_line=end_line,
        matched_text=matched,
        confidence=0.98,
        method="whitespace",
    )


# ── Layer 3: Fuzzy Line-by-Line Match ──────────────────────────

def _fuzzy_match(
    content: str,
    search_block: str,
    min_confidence: float = 0.85,
) -> MatchResult | None:
    """Layer 3: Sliding window fuzzy match using SequenceMatcher."""
    content_lines = content.split("\n")
    search_lines = search_block.split("\n")

    if not search_lines or not content_lines:
        return None

    n_search = len(search_lines)
    best_ratio = 0.0
    best_start = 0

    # Slide search_block window over content
    for i in range(len(content_lines) - n_search + 1):
        window = content_lines[i:i + n_search]

        # Compare joined text for overall similarity
        window_text = "\n".join(window)
        search_text = "\n".join(search_lines)

        ratio = difflib.SequenceMatcher(
            None, window_text, search_text, autojunk=False
        ).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_ratio < min_confidence:
        # Return best partial for error reporting even if below threshold
        if best_ratio > 0.3:
            matched = "\n".join(content_lines[best_start:best_start + n_search])
            return MatchResult(
                start_line=best_start,
                end_line=best_start + n_search,
                matched_text=matched,
                confidence=best_ratio,
                method="fuzzy",
            )
        return None

    matched = "\n".join(content_lines[best_start:best_start + n_search])
    return MatchResult(
        start_line=best_start,
        end_line=best_start + n_search,
        matched_text=matched,
        confidence=best_ratio,
        method="fuzzy",
    )


# ── Layer 4: Indentation-Agnostic Match ────────────────────────

def _strip_indent(lines: list[str]) -> list[str]:
    """Strip all leading whitespace from each line."""
    return [line.lstrip() for line in lines]


def _indent_agnostic_match(
    content: str,
    search_block: str,
    min_confidence: float = 0.85,
) -> MatchResult | None:
    """Layer 4: Match ignoring indentation differences."""
    content_lines = content.split("\n")
    search_lines = search_block.split("\n")

    if not search_lines or not content_lines:
        return None

    stripped_search = _strip_indent(search_lines)
    n_search = len(search_lines)

    best_ratio = 0.0
    best_start = 0

    for i in range(len(content_lines) - n_search + 1):
        window = content_lines[i:i + n_search]
        stripped_window = _strip_indent(window)

        window_text = "\n".join(stripped_window)
        search_text = "\n".join(stripped_search)

        ratio = difflib.SequenceMatcher(
            None, window_text, search_text, autojunk=False
        ).ratio()

        if ratio > best_ratio:
            best_ratio = ratio
            best_start = i

    if best_ratio < min_confidence:
        return None

    matched = "\n".join(content_lines[best_start:best_start + n_search])

    # Slight confidence penalty for indentation mismatch
    confidence = best_ratio * 0.97

    return MatchResult(
        start_line=best_start,
        end_line=best_start + n_search,
        matched_text=matched,
        confidence=confidence,
        method="indent_agnostic",
    )


# ── Helpers ────────────────────────────────────────────────────

def _reindent_replacement(
    original_lines: list[str],
    search_lines: list[str],
    replace_lines: list[str],
) -> list[str]:
    """Re-apply the indentation from the original content to the replacement.

    Determines the indentation offset between the search block and the
    original content, then applies that offset to the replacement block.
    """
    if not original_lines or not search_lines or not replace_lines:
        return replace_lines

    # Get the indent of the first non-empty line in original vs search
    orig_indent = _get_indent(original_lines)
    search_indent = _get_indent(search_lines)

    if orig_indent is None or search_indent is None:
        return replace_lines

    # Calculate indent difference
    diff = len(orig_indent) - len(search_indent)

    if diff == 0:
        return replace_lines

    result = []
    for line in replace_lines:
        if not line.strip():
            result.append(line)
            continue

        if diff > 0:
            # Add indentation
            result.append(" " * diff + line)
        else:
            # Remove indentation (carefully)
            current_indent = len(line) - len(line.lstrip())
            remove = min(abs(diff), current_indent)
            result.append(line[remove:])

    return result


def _get_indent(lines: list[str]) -> str | None:
    """Get the indentation of the first non-empty line."""
    for line in lines:
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return None
