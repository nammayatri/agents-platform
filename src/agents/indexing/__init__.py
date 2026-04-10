"""Code indexing and structural analysis package."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def build_indexes_and_repo_map(
    repo_path: str,
    *,
    cache_dir: str | None = None,
    repo_map_budget: int = 4000,
) -> str | None:
    """Build both the structural and embedding indexes, return the repo map.

    This is a synchronous, CPU-heavy function. Callers in async contexts
    MUST run it via ``asyncio.to_thread()`` to avoid blocking the event loop.

    Args:
        repo_path: Absolute path to the repository root (the ``repo/`` dir).
        cache_dir: Shared cache directory for persistent indexes. If None,
                   defaults to ``repo_path/.agent_index/``.
        repo_map_budget: Token budget for the rendered repo map.

    Returns:
        The rendered repo map string, or None if structural indexing failed
        or produced no symbols. The embedding index is built as a side effect
        regardless of the return value.
    """
    effective_cache = cache_dir or os.path.join(repo_path, ".agent_index")
    os.makedirs(effective_cache, exist_ok=True)

    repo_map_text: str | None = None

    # 1. Structural index (tree-sitter + PageRank repo map)
    try:
        from agents.indexing.indexer import RepoIndexer
        from agents.indexing.repo_map import render_repo_map

        indexer = RepoIndexer()
        graph = indexer.index(repo_path, cache_dir=effective_cache)
        if graph.symbol_count > 0:
            try:
                from agents.utils.token_counter import count_tokens
                count_fn = lambda t: count_tokens(t, "default")
            except ImportError:
                count_fn = lambda t: len(t) // 4

            repo_map_text = render_repo_map(
                graph,
                token_budget=repo_map_budget,
                count_tokens_fn=count_fn,
            )
            logger.info(
                "build_indexes: structural index done — %d symbols, %d files",
                graph.symbol_count, graph.file_count,
            )
    except ImportError:
        logger.debug("build_indexes: tree-sitter indexing not available")
    except Exception:
        logger.warning("build_indexes: structural indexing failed", exc_info=True)

    # Embedding index is built on-demand when semantic_search is called.
    # Don't build it eagerly here — it takes minutes on large repos and
    # blocks the server. The search_tool.get_or_build_index() handles
    # lazy loading from disk cache.

    return repo_map_text


# Files to sync between task and project index directories
_INDEX_FILES = ("mtimes.json", "embed_mtimes.json", "chunks.json", "faiss.idx")


def copy_project_index_to_task(project_index_dir: str, task_index_dir: str) -> bool:
    """Copy the project-level index to a task workspace as a starting base.

    Returns True if files were copied, False if no project index exists.
    """
    if not os.path.isdir(project_index_dir):
        return False

    has_files = any(
        os.path.exists(os.path.join(project_index_dir, f)) for f in _INDEX_FILES
    )
    if not has_files:
        return False

    import shutil

    os.makedirs(task_index_dir, exist_ok=True)
    copied = 0
    for fname in _INDEX_FILES:
        src = os.path.join(project_index_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(task_index_dir, fname))
            copied += 1

    logger.info(
        "copy_project_index_to_task: copied %d files from %s → %s",
        copied, project_index_dir, task_index_dir,
    )
    return copied > 0


def sync_task_index_to_project(task_index_dir: str, project_index_dir: str) -> None:
    """Sync task-level index files back to the project-level index.

    Overwrites project files with the task's updated versions so the next
    task starts with a warm base that includes changes from this task.
    """
    if not os.path.isdir(task_index_dir):
        return

    import shutil

    os.makedirs(project_index_dir, exist_ok=True)
    synced = 0
    for fname in _INDEX_FILES:
        src = os.path.join(task_index_dir, fname)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(project_index_dir, fname))
            synced += 1

    if synced:
        logger.info(
            "sync_task_index_to_project: synced %d files from %s → %s",
            synced, task_index_dir, project_index_dir,
        )
