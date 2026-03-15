"""Semantic search tool for agent use.

Exposes the embedding index as an agent tool that can be called
during task execution via the tool loop.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from agents.indexing.embeddings import EmbeddingIndex, SearchResult

logger = logging.getLogger(__name__)

# LRU index cache: cache_key -> EmbeddingIndex
# Capped to prevent unbounded memory growth (each index holds FAISS vectors
# + chunk metadata, typically 40-120MB per project).
from collections import OrderedDict

_MAX_CACHED_INDICES = 3
_index_cache: OrderedDict[str, EmbeddingIndex] = OrderedDict()


def _evict_lru() -> None:
    """Evict least-recently-used indices beyond the cap."""
    while len(_index_cache) > _MAX_CACHED_INDICES:
        oldest_key, idx = _index_cache.popitem(last=False)
        # Free heavy resources
        idx._index = None
        idx._chunks = []
        if hasattr(idx, "_embeddings"):
            idx._embeddings = None
        _stats.pop(oldest_key, None)
        logger.info("semantic_search: evicted index for %s (cache full)", oldest_key)


# ── Index stats tracking ──────────────────────────────────────────


@dataclass
class IndexStats:
    """Cumulative stats for index usage within a session."""
    searches: int = 0
    cache_hits: int = 0        # in-memory cache hits
    disk_loads: int = 0        # loaded from disk (faiss.idx)
    cold_builds: int = 0       # built from scratch
    total_results: int = 0
    avg_top_score: float = 0.0
    _score_sum: float = field(default=0.0, repr=False)

    def record_search(self, results: list[SearchResult]) -> None:
        self.searches += 1
        self.total_results += len(results)
        if results:
            top_score = results[0].score
            self._score_sum += top_score
            self.avg_top_score = self._score_sum / self.searches

    def to_dict(self) -> dict:
        return {
            "searches": self.searches,
            "cache_hits": self.cache_hits,
            "disk_loads": self.disk_loads,
            "cold_builds": self.cold_builds,
            "total_results": self.total_results,
            "avg_top_score": round(self.avg_top_score, 3),
        }


# Global stats per cache_key
_stats: dict[str, IndexStats] = {}

# Per-search metadata (keyed by cache_key, overwritten each search)
_last_search_meta: dict[str, dict] = {}


def get_index_stats(cache_key: str | None = None) -> dict:
    """Get index stats for a cache key, or all stats if key is None."""
    if cache_key:
        return _stats.get(cache_key, IndexStats()).to_dict()
    return {k: v.to_dict() for k, v in _stats.items()}


def pop_last_search_meta(cache_key: str) -> dict | None:
    """Pop the metadata from the most recent search for this cache key."""
    return _last_search_meta.pop(cache_key, None)


def get_or_build_index(workspace_path: str, *, cache_dir: str | None = None) -> EmbeddingIndex:
    """Get or build the embedding index for a workspace.

    Caches the index in memory so subsequent calls are fast.

    Args:
        workspace_path: Path to the repo to index.
        cache_dir: Optional shared cache directory. If not provided,
                   defaults to ``workspace_path/.agent_index/``.
    """
    cache_key = cache_dir or workspace_path
    stats = _stats.setdefault(cache_key, IndexStats())

    if cache_key in _index_cache:
        idx = _index_cache[cache_key]
        if idx._initialized:
            # Move to end (most recently used)
            _index_cache.move_to_end(cache_key)
            stats.cache_hits += 1
            return idx

    idx = EmbeddingIndex()

    # Try loading from disk first
    import os
    disk_cache = cache_dir or os.path.join(workspace_path, ".agent_index")
    if os.path.exists(os.path.join(disk_cache, "faiss.idx")):
        if idx.load(disk_cache):
            _index_cache[cache_key] = idx
            _evict_lru()
            stats.disk_loads += 1
            logger.info("semantic_search: loaded index from disk for %s", disk_cache)
            return idx

    # Build fresh index
    stats.cold_builds += 1
    idx.build_index(workspace_path, cache_dir=cache_dir)
    _index_cache[cache_key] = idx
    _evict_lru()
    return idx


def pre_warm_index(workspace_path: str, *, cache_dir: str | None = None) -> None:
    """Build the embedding index eagerly and populate the in-memory cache.

    Called during planning phase setup so that subsequent semantic_search
    tool calls find a warm cache and return instantly.

    This is a synchronous, potentially slow operation. Callers in async
    contexts should run it via ``asyncio.to_thread()``.
    """
    try:
        get_or_build_index(workspace_path, cache_dir=cache_dir)
        logger.info("pre_warm_index: embedding index ready for %s", cache_dir or workspace_path)
    except ImportError:
        logger.info("pre_warm_index: sentence-transformers/faiss not available, skipping")
    except Exception:
        logger.warning("pre_warm_index: failed to build embedding index", exc_info=True)


def execute_semantic_search(
    workspace_path: str,
    query: str,
    top_k: int = 10,
    *,
    cache_dir: str | None = None,
) -> tuple[str, dict]:
    """Execute a semantic search and return formatted results + metadata.

    This is the function called by the tool executor when an agent
    invokes the semantic_search tool.

    Args:
        workspace_path: Path to the repo to index/search.
        cache_dir: Optional shared cache directory for persistent indexes.

    Returns:
        ``(result_text, meta)`` — *meta* contains search stats for event publishing:
        ``{"results_count", "top_score", "latency_ms", "source"}``.
        *source* is ``"cache"`` | ``"disk"`` | ``"cold_build"`` | ``"error"``.
    """
    cache_key = cache_dir or workspace_path
    stats = _stats.get(cache_key)
    prev_cache = stats.cache_hits if stats else 0
    prev_disk = stats.disk_loads if stats else 0

    t0 = time.monotonic()

    try:
        index = get_or_build_index(workspace_path, cache_dir=cache_dir)
        results = index.search(query, top_k=top_k)
    except ImportError as e:
        msg = f"Semantic search unavailable: {e}. Use search_files tool instead."
        return msg, {"results_count": 0, "top_score": 0, "latency_ms": 0, "source": "error"}
    except Exception as e:
        logger.error("semantic_search failed: %s", e)
        msg = f"Semantic search error: {e}. Use search_files tool instead."
        return msg, {"results_count": 0, "top_score": 0, "latency_ms": 0, "source": "error"}

    latency_ms = int((time.monotonic() - t0) * 1000)

    # Record search in stats
    stats = _stats.get(cache_key)
    if stats:
        stats.record_search(results)

    # Determine how the index was obtained
    if stats and stats.cache_hits > prev_cache:
        source = "cache"
    elif stats and stats.disk_loads > prev_disk:
        source = "disk"
    else:
        source = "cold_build"

    top_score = round(results[0].score, 3) if results else 0
    meta = {
        "results_count": len(results),
        "top_score": top_score,
        "latency_ms": latency_ms,
        "source": source,
        "query": query[:100],
    }

    # Store meta for event publishing (coordinator reads this via pop_last_search_meta)
    _last_search_meta[cache_key] = meta

    if not results:
        return "No results found for the query. Try rephrasing or use search_files for exact pattern matching.", meta

    return _format_results(results, query), meta


def execute_multi_repo_search(
    workspace_path: str,
    query: str,
    top_k: int = 10,
    *,
    cache_dir: str | None = None,
    dep_index_dirs: dict[str, str] | None = None,
) -> tuple[str, dict]:
    """Execute semantic search across main repo and all dependency repos.

    Searches each repo's index independently, merges results by score,
    and tags each result with its repo name.

    Args:
        workspace_path: Path to the main repo.
        query: Natural language search query.
        top_k: Total results to return across all repos.
        cache_dir: Cache dir for the main repo index.
        dep_index_dirs: Mapping of dep_name -> index_dir for dependency repos.

    Returns:
        ``(result_text, meta)`` — same format as execute_semantic_search.
    """
    t0 = time.monotonic()
    all_results: list[tuple[str, SearchResult]] = []  # (repo_name, result)

    # Search main repo
    try:
        main_index = get_or_build_index(workspace_path, cache_dir=cache_dir)
        main_results = main_index.search(query, top_k=top_k)
        for r in main_results:
            all_results.append(("main", r))
    except Exception as e:
        logger.warning("multi_repo_search: main repo search failed: %s", e)

    # Search each dependency repo
    if dep_index_dirs:
        for dep_name, dep_idx_dir in dep_index_dirs.items():
            try:
                dep_index = get_or_build_index(dep_idx_dir, cache_dir=dep_idx_dir)
                dep_results = dep_index.search(query, top_k=top_k)
                for r in dep_results:
                    all_results.append((dep_name, r))
            except Exception as e:
                logger.warning(
                    "multi_repo_search: dep %s search failed: %s", dep_name, e,
                )

    latency_ms = int((time.monotonic() - t0) * 1000)

    # Sort by score descending and take top_k
    all_results.sort(key=lambda x: x[1].score, reverse=True)
    all_results = all_results[:top_k]

    top_score = round(all_results[0][1].score, 3) if all_results else 0
    repos_searched = 1 + (len(dep_index_dirs) if dep_index_dirs else 0)
    meta = {
        "results_count": len(all_results),
        "top_score": top_score,
        "latency_ms": latency_ms,
        "source": "multi_repo",
        "query": query[:100],
        "repos_searched": repos_searched,
    }

    cache_key = cache_dir or workspace_path
    _last_search_meta[cache_key] = meta

    if not all_results:
        return "No results found across any repo. Try rephrasing or use search_files for exact pattern matching.", meta

    return _format_multi_repo_results(all_results, query), meta


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


def _format_multi_repo_results(
    results: list[tuple[str, SearchResult]], query: str,
) -> str:
    """Format multi-repo search results with repo labels."""
    lines = [f"Semantic search results for: \"{query}\" (across all repos)", ""]

    for i, (repo_name, r) in enumerate(results, 1):
        score_pct = int(r.score * 100)
        repo_label = f"[{repo_name}]" if repo_name != "main" else "[main repo]"
        lines.append(f"--- Result {i} {repo_label} (relevance: {score_pct}%) ---")
        lines.append(f"File: {r.file_path} (lines {r.line_start}-{r.line_end})")
        lines.append(r.snippet)
        lines.append("")

    return "\n".join(lines)


# Tool definition for the agent registry
SEMANTIC_SEARCH_TOOL = {
    "name": "semantic_search",
    "description": (
        "Search the codebase semantically using natural language across the main repo "
        "and all configured dependency repos. "
        "Use this when you need to find code related to a concept, feature, or pattern. "
        "More powerful than search_files/grep for conceptual queries like "
        "'where is authentication handled' or 'error handling patterns'. "
        "Results are labeled with their source repo. "
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
