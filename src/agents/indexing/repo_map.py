"""Render a token-budget-aware repository map.

Takes a SymbolGraph and renders a concise, ranked map of the most important
symbols in the codebase. Uses binary search to fit within a token budget.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from agents.indexing.symbol_graph import SymbolGraph, RankedSymbol

logger = logging.getLogger(__name__)


def render_repo_map(
    graph: SymbolGraph,
    token_budget: int = 4000,
    count_tokens_fn=None,
) -> str:
    """Render a repo map that fits within the token budget.

    Args:
        graph: The symbol graph to render.
        token_budget: Maximum tokens for the output.
        count_tokens_fn: Optional function (str) -> int for counting tokens.
            If not provided, uses len(text) // 4 as heuristic.

    Returns:
        Formatted repo map string.
    """
    if count_tokens_fn is None:
        count_tokens_fn = lambda text: len(text) // 4

    all_symbols = graph.get_ranked_symbols()

    if not all_symbols:
        return "# Repository Map\n\nNo symbols found."

    # Binary search for the right number of symbols that fits the budget
    lo, hi = 1, len(all_symbols)
    best_n = 1

    while lo <= hi:
        mid = (lo + hi) // 2
        rendered = _render_symbols(all_symbols[:mid])
        tokens = count_tokens_fn(rendered)

        if tokens <= token_budget:
            best_n = mid
            lo = mid + 1
        else:
            hi = mid - 1

    result = _render_symbols(all_symbols[:best_n])

    total = graph.symbol_count
    if best_n < total:
        result += f"\n\n# ... {total - best_n} more symbols (truncated to fit budget)\n"

    logger.info(
        "repo_map: rendered %d/%d symbols, %d tokens (budget=%d)",
        best_n, total, count_tokens_fn(result), token_budget,
    )

    return result


def _render_symbols(ranked_symbols: list[RankedSymbol]) -> str:
    """Render a list of ranked symbols grouped by file."""
    # Group symbols by file, maintaining rank order
    file_symbols: dict[str, list[RankedSymbol]] = defaultdict(list)
    file_order: list[str] = []

    for rs in ranked_symbols:
        fp = rs.symbol.file_path
        if fp not in file_symbols:
            file_order.append(fp)
        file_symbols[fp].append(rs)

    lines = ["# Repository Map", ""]

    for file_path in file_order:
        symbols = file_symbols[file_path]
        # Sort symbols within file by line number
        symbols.sort(key=lambda r: r.symbol.line)

        lines.append(file_path)
        for rs in symbols:
            s = rs.symbol
            ref_info = f" ({rs.references} refs)" if rs.references > 0 else ""
            lines.append(f"  {s.kind} {s.name} [L{s.line}]{ref_info}")
        lines.append("")

    return "\n".join(lines)
