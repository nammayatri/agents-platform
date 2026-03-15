"""Context window budget allocation and enforcement."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from agents.utils.token_counter import count_tokens, get_context_window

logger = logging.getLogger(__name__)


@dataclass
class BudgetAllocation:
    """Token budget allocation across context sections."""
    total: int
    system_prompt: int
    repo_map: int
    file_tree: int
    iteration_history: int
    memories: int
    embeddings_context: int
    working_context: int  # remainder for current files, tool results

    def log_summary(self) -> str:
        """Return a human-readable summary of budget allocation."""
        parts = [
            f"total={self.total}",
            f"system={self.system_prompt}",
            f"repo_map={self.repo_map}",
            f"file_tree={self.file_tree}",
            f"iteration_history={self.iteration_history}",
            f"memories={self.memories}",
            f"embeddings={self.embeddings_context}",
            f"working={self.working_context}",
        ]
        return ", ".join(parts)


def get_budget(model: str) -> BudgetAllocation:
    """Calculate budget allocation for a given model.

    Allocates fixed budgets for structured sections and gives
    the remainder to working_context (current files, tool results).
    """
    total = get_context_window(model)

    # Fixed allocations (tokens)
    system_prompt = 2_000
    repo_map = 4_000
    file_tree = 1_000
    iteration_history = 3_000
    memories = 1_000
    embeddings_context = 2_000

    # Scale allocations for smaller context windows
    if total < 64_000:
        # Smaller models: reduce allocations proportionally
        scale = total / 128_000
        repo_map = int(repo_map * scale)
        iteration_history = int(iteration_history * scale)
        embeddings_context = int(embeddings_context * scale)

    fixed_total = (
        system_prompt + repo_map + file_tree +
        iteration_history + memories + embeddings_context
    )

    # Reserve 20% of total as minimum for working context
    min_working = total // 5

    # If fixed allocations leave too little for working context, scale them down
    if total - fixed_total < min_working:
        # Reduce proportionally, keeping system_prompt fixed
        reducible = fixed_total - system_prompt
        available = total - system_prompt - min_working
        if reducible > 0 and available > 0:
            factor = available / reducible
            repo_map = int(repo_map * factor)
            file_tree = int(file_tree * factor)
            iteration_history = int(iteration_history * factor)
            memories = int(memories * factor)
            embeddings_context = int(embeddings_context * factor)

    working = total - (
        system_prompt + repo_map + file_tree +
        iteration_history + memories + embeddings_context
    )

    return BudgetAllocation(
        total=total,
        system_prompt=system_prompt,
        repo_map=repo_map,
        file_tree=file_tree,
        iteration_history=iteration_history,
        memories=memories,
        embeddings_context=embeddings_context,
        working_context=max(working, 0),
    )


def truncate_to_budget(
    text: str,
    budget: int,
    model: str = "default",
    strategy: str = "tail",
) -> str:
    """Truncate text to fit within a token budget.

    Strategies:
        - "tail": keep the end (most recent content)
        - "head": keep the beginning
        - "middle": keep start + end, cut middle
    """
    if not text:
        return text

    current_tokens = count_tokens(text, model)
    if current_tokens <= budget:
        return text

    lines = text.split("\n")

    if strategy == "head":
        return _truncate_head(lines, budget, model)
    elif strategy == "middle":
        return _truncate_middle(lines, budget, model)
    else:  # "tail"
        return _truncate_tail(lines, budget, model)


def _truncate_head(lines: list[str], budget: int, model: str) -> str:
    """Keep lines from the beginning until budget is reached."""
    result = []
    tokens = 0
    for line in lines:
        line_tokens = count_tokens(line + "\n", model)
        if tokens + line_tokens > budget:
            break
        result.append(line)
        tokens += line_tokens
    result.append(f"\n... [truncated, {len(lines) - len(result)} lines omitted]")
    return "\n".join(result)


def _truncate_tail(lines: list[str], budget: int, model: str) -> str:
    """Keep lines from the end until budget is reached."""
    result = []
    tokens = 0
    for line in reversed(lines):
        line_tokens = count_tokens(line + "\n", model)
        if tokens + line_tokens > budget:
            break
        result.append(line)
        tokens += line_tokens
    omitted = len(lines) - len(result)
    result.reverse()
    if omitted > 0:
        result.insert(0, f"[... {omitted} lines omitted ...]")
    return "\n".join(result)


def _truncate_middle(lines: list[str], budget: int, model: str) -> str:
    """Keep start and end, cut the middle."""
    if len(lines) <= 2:
        return _truncate_head(lines, budget, model)

    # Allocate 40% to head, 60% to tail (recent content more important)
    head_budget = int(budget * 0.4)
    tail_budget = budget - head_budget

    head_lines = []
    head_tokens = 0
    for line in lines:
        lt = count_tokens(line + "\n", model)
        if head_tokens + lt > head_budget:
            break
        head_lines.append(line)
        head_tokens += lt

    tail_lines = []
    tail_tokens = 0
    for line in reversed(lines):
        lt = count_tokens(line + "\n", model)
        if tail_tokens + lt > tail_budget:
            break
        tail_lines.append(line)
        tail_tokens += lt
    tail_lines.reverse()

    omitted = len(lines) - len(head_lines) - len(tail_lines)
    if omitted > 0:
        return "\n".join(head_lines) + f"\n\n[... {omitted} lines omitted ...]\n\n" + "\n".join(tail_lines)
    return "\n".join(lines)
