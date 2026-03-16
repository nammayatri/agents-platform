"""Semantic search tool for agent use.

Exposes the embedding index as an agent tool that can be called
during task execution via the tool loop.
"""

from __future__ import annotations

import logging

from agents.indexing.embeddings import EmbeddingIndex, SearchResult

logger = logging.getLogger(__name__)

# Singleton index cache: workspace_path -> EmbeddingIndex
_index_cache: dict[str, EmbeddingIndex] = {}


def get_or_build_index(workspace_path: str, *, cache_dir: str | None = None) -> EmbeddingIndex:
    """Get or build the embedding index for a workspace.

    Caches the index in memory so subsequent calls are fast.

    Args:
        workspace_path: Path to the repo to index.
        cache_dir: Optional shared cache directory. If not provided,
                   defaults to ``workspace_path/.agent_index/``.
    """
    cache_key = cache_dir or workspace_path
    if cache_key in _index_cache:
        idx = _index_cache[cache_key]
        if idx._initialized:
            return idx

    idx = EmbeddingIndex()

    # Try loading from disk first
    import os
    disk_cache = cache_dir or os.path.join(workspace_path, ".agent_index")
    if os.path.exists(os.path.join(disk_cache, "faiss.idx")):
        if idx.load(disk_cache):
            _index_cache[cache_key] = idx
            logger.info("semantic_search: loaded index from disk for %s", disk_cache)
            return idx

    # Build fresh index
    idx.build_index(workspace_path, cache_dir=cache_dir)
    _index_cache[cache_key] = idx
    return idx


def execute_semantic_search(workspace_path: str, query: str, top_k: int = 10, *, cache_dir: str | None = None) -> str:
    """Execute a semantic search and return formatted results.

    This is the function called by the tool executor when an agent
    invokes the semantic_search tool.

    Args:
        workspace_path: Path to the repo to index/search.
        cache_dir: Optional shared cache directory for persistent indexes.
    """
    try:
        index = get_or_build_index(workspace_path, cache_dir=cache_dir)
        results = index.search(query, top_k=top_k)
    except ImportError as e:
        return f"Semantic search unavailable: {e}. Use search_files tool instead."
    except Exception as e:
        logger.error("semantic_search failed: %s", e)
        return f"Semantic search error: {e}. Use search_files tool instead."

    if not results:
        return "No results found for the query. Try rephrasing or use search_files for exact pattern matching."

    return _format_results(results, query)


def _format_results(results: list[SearchResult], query: str) -> str:
    """Format search results for the agent."""
    lines = [f"Semantic search results for: \"{query}\"", ""]

    for i, r in enumerate(results, 1):
        score_pct = int(r.score * 100)
        lines.append(f"--- Result {i} (relevance: {score_pct}%) ---")
        lines.append(f"File: {r.file_path} (lines {r.line_start}-{r.line_end})")
        lines.append(r.snippet)
        lines.append("")

    return "\n".join(lines)


# Tool definition for the agent registry
SEMANTIC_SEARCH_TOOL = {
    "name": "semantic_search",
    "description": (
        "Search the codebase semantically using natural language. "
        "Use this when you need to find code related to a concept, feature, or pattern. "
        "More powerful than search_files/grep for conceptual queries like "
        "'where is authentication handled' or 'error handling patterns'. "
        "For exact string matching, use search_files instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language description of what to find in the codebase",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default: 10)",
                "default": 10,
            },
        },
        "required": ["query"],
    },
}
