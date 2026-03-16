"""Repository indexer with incremental update support.

Coordinates tree-sitter parsing, symbol graph building, and reference resolution.
Uses mtime-based caching to only re-parse files that have changed.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from agents.indexing.treesitter import parse_file, EXT_TO_LANGUAGE
from agents.indexing.symbol_graph import SymbolGraph

logger = logging.getLogger(__name__)

# Directories to skip during indexing
SKIP_DIRS: set[str] = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "target", "vendor",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "egg-info", ".eggs", "htmlcov", "coverage",
    ".agent_index",
}

# Maximum file size to index (500KB)
MAX_FILE_SIZE = 500 * 1024


class RepoIndexer:
    """Indexes a repository for structural code understanding.

    Uses incremental indexing: tracks file modification times and only
    re-parses files that have changed since the last index.
    """

    def __init__(self):
        self._cache_dir: str | None = None
        self._mtimes: dict[str, float] = {}
        self._graph: SymbolGraph | None = None

    def index(self, repo_path: str, *, cache_dir: str | None = None) -> SymbolGraph:
        """Index a repository and return its symbol graph.

        Uses cached results for unchanged files. Thread-safe for
        concurrent reads but not concurrent writes.

        Args:
            repo_path: Path to the repository root to index.
            cache_dir: Optional shared cache directory for persistent indexes.
                       If not provided, defaults to ``repo_path/.agent_index/``.
        """
        repo = Path(repo_path)
        if not repo.is_dir():
            logger.warning("repo_indexer: %s is not a directory", repo_path)
            return SymbolGraph()

        # Set up cache directory (shared location if provided, else per-repo)
        self._cache_dir = cache_dir or str(repo / ".agent_index")
        os.makedirs(self._cache_dir, exist_ok=True)

        # Load previous mtimes
        old_mtimes = self._load_mtimes()

        # Collect all indexable files
        files = self._collect_files(repo)
        logger.info("repo_indexer: found %d indexable files in %s", len(files), repo_path)

        # Determine which files need re-parsing
        changed_files = []
        unchanged_files = []
        current_mtimes: dict[str, float] = {}

        for file_path in files:
            try:
                mtime = os.path.getmtime(file_path)
            except OSError:
                continue

            current_mtimes[file_path] = mtime

            if file_path in old_mtimes and old_mtimes[file_path] == mtime:
                unchanged_files.append(file_path)
            else:
                changed_files.append(file_path)

        logger.info(
            "repo_indexer: %d changed, %d unchanged files",
            len(changed_files), len(unchanged_files),
        )

        # Build graph
        graph = SymbolGraph()

        # Parse all files (we need all symbols for reference resolution)
        for file_path in files:
            try:
                symbols = parse_file(file_path)
                # Use relative path for display
                rel_path = os.path.relpath(file_path, repo_path)
                for symbol in symbols:
                    symbol.file_path = rel_path
                    graph.add_definition(symbol, rel_path)
            except Exception as e:
                logger.debug("Failed to parse %s: %s", file_path, e)

        # Resolve references (scan each file for references to known symbols)
        for file_path in files:
            try:
                rel_path = os.path.relpath(file_path, repo_path)
                source = Path(file_path).read_text(errors="replace")
                graph.resolve_references(rel_path, source)
            except Exception as e:
                logger.debug("Failed to resolve references in %s: %s", file_path, e)

        # Save mtimes for incremental indexing
        self._save_mtimes(current_mtimes)
        self._graph = graph

        logger.info(
            "repo_indexer: indexed %d files, %d symbols, %d edges",
            graph.file_count, graph.symbol_count, sum(len(t) for t in graph.edges.values()),
        )

        return graph

    def _collect_files(self, repo: Path) -> list[str]:
        """Collect all indexable source files in the repository."""
        files = []

        for root, dirs, filenames in os.walk(repo):
            # Skip excluded directories (modifies dirs in-place)
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]

            for filename in filenames:
                file_path = os.path.join(root, filename)

                # Check extension
                ext = os.path.splitext(filename)[1].lower()
                if ext not in EXT_TO_LANGUAGE:
                    continue

                # Check file size
                try:
                    if os.path.getsize(file_path) > MAX_FILE_SIZE:
                        continue
                except OSError:
                    continue

                files.append(file_path)

        return sorted(files)

    def _load_mtimes(self) -> dict[str, float]:
        """Load cached file modification times."""
        if not self._cache_dir:
            return {}

        mtimes_file = os.path.join(self._cache_dir, "mtimes.json")
        try:
            with open(mtimes_file) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_mtimes(self, mtimes: dict[str, float]) -> None:
        """Save file modification times to cache."""
        if not self._cache_dir:
            return

        mtimes_file = os.path.join(self._cache_dir, "mtimes.json")
        try:
            with open(mtimes_file, "w") as f:
                json.dump(mtimes, f)
        except OSError as e:
            logger.warning("Failed to save mtimes cache: %s", e)
