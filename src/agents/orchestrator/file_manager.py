"""Workspace file manager — single source of truth for path resolution and access control.

Every agent operates within a workspace. This class owns:
1. Path resolution: relative paths → absolute paths
2. Access control: what the agent can read vs write
3. Repo identification: which repo does a path belong to
4. Context discovery: what deps, context docs, sibling repos exist

Layout (from the agent's perspective, working in task_root/{repo_name}/):
    .              → the repo the agent is working in (repo_dir)
    ../deps/{name}/ → read-only dependency repos (symlink to project level)
    ../.context/    → project context docs
    ../{name}/      → sibling writeable repos in this task
    ../indexes/     → search indexes (internal, not exposed to agents)

Usage:
    fm = WorkspaceFileManager(workspace_path)
    abs_path = fm.resolve("src/main.py")           # → repo_dir/src/main.py
    abs_path = fm.resolve("../deps/auth/api.py")   # → task_root/deps/auth/api.py
    fm.check_read(abs_path)                         # raises if not allowed
    fm.check_write(abs_path)                        # raises if not in repo_dir
    label = fm.identify(abs_path)                   # → "main", "auth", "context-docs"
"""

from __future__ import annotations

import os
import logging

logger = logging.getLogger(__name__)

# Directories at task_root that are NOT repo workspaces
_INTERNAL_DIRS = frozenset({"deps", ".context", "indexes"})


class WorkspaceFileManager:
    """Manages file access for a single agent workspace."""

    def __init__(self, workspace_path: str):
        """
        Args:
            workspace_path: The git working directory (e.g. task_root/main/).
        """
        self.repo_dir = os.path.realpath(workspace_path)
        self.repo_name = os.path.basename(workspace_path)
        self.task_root = os.path.realpath(os.path.join(workspace_path, ".."))

        # Resolve deps (may be a symlink)
        deps_path = os.path.join(self.task_root, "deps")
        self._deps_real = os.path.realpath(deps_path) if os.path.exists(deps_path) else None

        # Build allowed dirs for read and write
        self._write_allowed = [self.repo_dir]
        self._read_allowed = [
            self.repo_dir,
            self.task_root,
        ]
        if self._deps_real:
            self._read_allowed.append(self._deps_real)

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def resolve(self, relative_path: str) -> str:
        """Resolve a relative path to an absolute path.

        Paths are relative to repo_dir. Supports ../ for accessing
        deps, context, and sibling repos.
        """
        full = os.path.normpath(os.path.join(self.repo_dir, relative_path))
        return os.path.realpath(full)

    # ------------------------------------------------------------------
    # Access control
    # ------------------------------------------------------------------

    def check_read(self, real_path: str) -> None:
        """Raise ValueError if the path is not readable."""
        if not self._is_under_any(real_path, self._read_allowed):
            raise ValueError(f"Read access denied: path outside workspace")

    def check_write(self, real_path: str) -> None:
        """Raise ValueError if the path is not writeable."""
        if not self._is_under_any(real_path, self._write_allowed):
            raise ValueError(f"Write access denied: path outside repo")

    def can_read(self, real_path: str) -> bool:
        """Check if a path is readable without raising."""
        return self._is_under_any(real_path, self._read_allowed)

    def can_write(self, real_path: str) -> bool:
        """Check if a path is writeable without raising."""
        return self._is_under_any(real_path, self._write_allowed)

    # ------------------------------------------------------------------
    # Repo identification
    # ------------------------------------------------------------------

    def identify(self, real_path: str) -> str:
        """Identify which repository/section a path belongs to.

        Returns: "main", "auth-service", "context-docs", "dependency", etc.
        """
        # Context docs
        ctx_dir = os.path.join(self.task_root, ".context")
        if self._is_under(real_path, ctx_dir):
            return "context-docs"

        # Deps (follows symlink)
        if self._deps_real and self._is_under(real_path, self._deps_real):
            rel = real_path[len(self._deps_real):].lstrip(os.sep)
            name = rel.split(os.sep, 1)[0] if rel else ""
            return name or "dependency"

        # Repos under task_root
        if self._is_under(real_path, self.task_root):
            rel = real_path[len(self.task_root):].lstrip(os.sep)
            name = rel.split(os.sep, 1)[0] if rel else ""
            if name and name not in _INTERNAL_DIRS and not name.startswith("."):
                return name

        return self.repo_name

    # ------------------------------------------------------------------
    # Discovery — what's available in this workspace
    # ------------------------------------------------------------------

    def list_deps(self) -> list[str]:
        """List available read-only dependency repo names."""
        deps_dir = os.path.join(self.task_root, "deps")
        if not os.path.isdir(deps_dir):
            return []
        return sorted(
            d for d in os.listdir(deps_dir)
            if os.path.isdir(os.path.join(deps_dir, d))
        )

    def list_sibling_repos(self) -> list[str]:
        """List other writeable repos in this task."""
        return sorted(
            d for d in os.listdir(self.task_root)
            if d != self.repo_name
            and d not in _INTERNAL_DIRS
            and not d.startswith(".")
            and os.path.isdir(os.path.join(self.task_root, d))
            and os.path.isdir(os.path.join(self.task_root, d, ".git"))
        )

    def has_context_docs(self) -> bool:
        """Check if .context/ directory exists."""
        return os.path.isdir(os.path.join(self.task_root, ".context"))

    def list_context_files(self) -> list[str]:
        """List available context doc paths (relative to repo_dir)."""
        ctx_dir = os.path.join(self.task_root, ".context")
        if not os.path.isdir(ctx_dir):
            return []
        result = []
        if os.path.isfile(os.path.join(ctx_dir, "UNDERSTANDING.md")):
            result.append("../.context/UNDERSTANDING.md")
        if os.path.isfile(os.path.join(ctx_dir, "LINKING.md")):
            result.append("../.context/LINKING.md")
        deps_ctx = os.path.join(ctx_dir, "deps")
        if os.path.isdir(deps_ctx):
            for f in sorted(os.listdir(deps_ctx)):
                if f.endswith(".md"):
                    result.append(f"../.context/deps/{f}")
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_under(path: str, parent: str) -> bool:
        """Check if path is under parent (using realpath comparison)."""
        rp = os.path.realpath(parent)
        return path.startswith(rp + os.sep) or path == rp

    @staticmethod
    def _is_under_any(path: str, parents: list[str]) -> bool:
        """Check if path is under any of the parent directories."""
        return any(
            path.startswith(p + os.sep) or path == p
            for p in parents
        )
