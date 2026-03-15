"""Workspace manager for server-side repository cloning and task-level workspaces.

Directory structure:
    {WORKSPACE_ROOT}/
        {project_id}/
            repo/              <- main project repo clone
            deps/
                {dep_name}/    <- cloned dependency repos
            analysis.json      <- cached codebase understanding
            tasks/
                {todo_id}/
                    repo/      <- fresh clone for this task (own branch)
"""

import asyncio
import json
import logging
import os
import shutil

import asyncpg

from agents.infra.crypto import decrypt
from agents.orchestrator.git_providers.factory import (
    build_clone_url,
    create_git_provider,
    detect_provider_type,
    parse_repo_url,
)

logger = logging.getLogger(__name__)


class WorkspaceManager:
    def __init__(self, db: asyncpg.Pool, workspace_root: str):
        self.db = db
        self.workspace_root = workspace_root
        os.makedirs(workspace_root, exist_ok=True)

    # ------------------------------------------------------------------
    # Project workspace
    # ------------------------------------------------------------------

    async def _resolve_git_credentials(
        self, git_provider_id: str | None, repo_url: str
    ) -> tuple[str | None, str | None]:
        """Resolve token and provider_type from a git_provider_configs reference.

        Returns (token, provider_type).
        """
        if git_provider_id:
            row = await self.db.fetchrow(
                "SELECT provider_type, api_base_url, token_enc "
                "FROM git_provider_configs WHERE id = $1 AND is_active = TRUE",
                git_provider_id,
            )
            if row:
                token = decrypt(row["token_enc"]) if row.get("token_enc") else None
                return token, row["provider_type"]

        # Fallback: auto-detect from URL, no token
        return None, detect_provider_type(repo_url)

    async def setup_project_workspace(self, project_id: str) -> str:
        """Clone/pull the main repo and dependency repos into the project workspace.

        Returns the project workspace path and stores it in the DB.
        """
        project = await self.db.fetchrow(
            "SELECT * FROM projects WHERE id = $1", project_id
        )
        if not project:
            raise ValueError(f"Project {project_id} not found")

        repo_url = project.get("repo_url")
        if not repo_url:
            raise ValueError(f"Project {project_id} has no repo_url")

        project_dir = os.path.join(self.workspace_root, str(project_id))
        repo_dir = os.path.join(project_dir, "repo")
        deps_dir = os.path.join(project_dir, "deps")
        os.makedirs(deps_dir, exist_ok=True)

        # Resolve credentials from git_provider_configs
        project_git_provider_id = str(project["git_provider_id"]) if project.get("git_provider_id") else None
        token, provider_type = await self._resolve_git_credentials(project_git_provider_id, repo_url)
        branch = project.get("default_branch") or "main"

        # Clone or pull the main repo
        await self._clone_or_pull(
            repo_url=repo_url,
            target_dir=repo_dir,
            branch=branch,
            token=token,
            provider_type=provider_type,
        )

        # Clone dependency repos
        context_docs = project.get("context_docs") or []
        if isinstance(context_docs, str):
            context_docs = json.loads(context_docs)

        for dep in context_docs:
            dep_url = dep.get("repo_url")
            if not dep_url:
                continue
            dep_name = dep.get("name", "").replace("/", "_").replace(" ", "_") or "dep"
            dep_dir = os.path.join(deps_dir, dep_name)
            try:
                # Each dependency can have its own git provider
                dep_git_provider_id = dep.get("git_provider_id")
                if dep_git_provider_id:
                    dep_token, dep_provider_type = await self._resolve_git_credentials(
                        dep_git_provider_id, dep_url
                    )
                else:
                    # Fallback to project's git provider for same-host deps, else auto-detect
                    dep_detected = detect_provider_type(dep_url)
                    if dep_detected == provider_type and token:
                        dep_token, dep_provider_type = token, provider_type
                    else:
                        dep_token, dep_provider_type = None, dep_detected

                await self._clone_or_pull(
                    repo_url=dep_url,
                    target_dir=dep_dir,
                    branch="HEAD",
                    token=dep_token,
                    provider_type=dep_provider_type,
                )
            except Exception:
                logger.warning("Failed to clone dependency %s from %s", dep_name, dep_url)

        # Store workspace path in DB
        await self.db.execute(
            "UPDATE projects SET workspace_path = $2, updated_at = NOW() WHERE id = $1",
            project_id,
            project_dir,
        )

        return project_dir

    # ------------------------------------------------------------------
    # Task workspace
    # ------------------------------------------------------------------

    async def setup_task_workspace(self, todo_id: str) -> str:
        """Create a task-level workspace with its own clone and branch.

        Clones from the project workspace (local clone for speed), creates a
        new branch named task/{short_id}, and returns the task workspace path.
        """
        logger.info("[workspace] setup_task_workspace START for todo=%s", todo_id)
        todo = await self.db.fetchrow(
            "SELECT t.*, p.repo_url, p.default_branch, p.git_provider_id, "
            "p.workspace_path AS project_workspace "
            "FROM todo_items t JOIN projects p ON t.project_id = p.id "
            "WHERE t.id = $1",
            todo_id,
        )
        if not todo:
            raise ValueError(f"Todo {todo_id} not found")

        project_id = str(todo["project_id"])
        project_workspace = todo.get("project_workspace")

        # Ensure project workspace exists
        if not project_workspace or not os.path.isdir(project_workspace):
            logger.info("[workspace] No project workspace, setting up for project=%s", project_id)
            project_workspace = await self.setup_project_workspace(project_id)

        project_repo_dir = os.path.join(project_workspace, "repo")
        if not os.path.isdir(project_repo_dir):
            raise ValueError(f"Project repo not cloned at {project_repo_dir}")

        # Create task workspace
        short_id = str(todo_id)[:8]
        task_dir = os.path.join(project_workspace, "tasks", str(todo_id))
        task_repo_dir = os.path.join(task_dir, "repo")

        if os.path.isdir(task_repo_dir):
            # Already exists -- pull latest
            logger.info("[workspace] Task workspace exists, fetching latest for todo=%s", todo_id)
            await self._run_git("fetch", "origin", cwd=task_repo_dir)
            return task_dir

        os.makedirs(task_dir, exist_ok=True)

        # Resolve git credentials from git_provider_configs
        clone_url = todo.get("repo_url") or ""
        git_provider_id = str(todo["git_provider_id"]) if todo.get("git_provider_id") else None
        logger.info("[workspace] Cloning task workspace: todo=%s clone_url=%s", todo_id, clone_url[:50] if clone_url else "none")
        token, provider_type = await self._resolve_git_credentials(git_provider_id, clone_url)

        # Use local clone from project workspace for speed
        rc, out = await self._run_git(
            "clone", project_repo_dir, task_repo_dir,
            cwd=self.workspace_root,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to clone task workspace: {out}")

        # Set the remote to the real origin (not the local path)
        authenticated_url = build_clone_url(clone_url, token, provider_type)
        await self._run_git(
            "remote", "set-url", "origin", authenticated_url,
            cwd=task_repo_dir,
        )

        # Create task branch
        branch_name = f"task/{short_id}"
        logger.info("[workspace] Created task branch=%s for todo=%s", branch_name, todo_id)
        await self._run_git("checkout", "-b", branch_name, cwd=task_repo_dir)

        # Symlink deps into task workspace
        project_deps = os.path.join(project_workspace, "deps")
        task_deps = os.path.join(task_dir, "deps")
        if os.path.isdir(project_deps) and not os.path.exists(task_deps):
            os.symlink(project_deps, task_deps)

        return task_dir

    async def cleanup_task_workspace(self, todo_id: str) -> None:
        """Remove a task workspace after completion."""
        todo = await self.db.fetchrow(
            "SELECT project_id FROM todo_items WHERE id = $1", todo_id
        )
        if not todo:
            return

        project_dir = os.path.join(self.workspace_root, str(todo["project_id"]))
        task_dir = os.path.join(project_dir, "tasks", str(todo_id))

        if os.path.isdir(task_dir):
            shutil.rmtree(task_dir, ignore_errors=True)
            logger.info("Cleaned up task workspace %s", task_dir)

    # ------------------------------------------------------------------
    # Command execution (quality checks, etc.)
    # ------------------------------------------------------------------

    async def run_command(
        self, cmd: str, cwd: str, *, timeout: int = 60,
    ) -> tuple[int, str]:
        """Run a shell command in the given directory.

        Returns (exit_code, combined_output).
        """
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
            return proc.returncode or 0, stdout.decode(errors="replace")
        except asyncio.TimeoutError:
            proc.kill()
            return 124, f"Command timed out after {timeout}s: {cmd}"

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    async def commit_and_push(
        self,
        workspace_path: str,
        message: str,
        branch: str,
    ) -> bool:
        """Stage all changes, commit, and push to remote."""
        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        # Stage all changes
        rc, out = await self._run_git("add", "-A", cwd=repo_dir)
        if rc != 0:
            logger.error("git add failed: %s", out)
            return False

        # Check if there's anything to commit
        rc, out = await self._run_git("diff", "--cached", "--quiet", cwd=repo_dir)
        if rc == 0:
            logger.info("No changes to commit")
            return True  # nothing to commit is not a failure

        # Commit
        rc, out = await self._run_git(
            "commit", "-m", message,
            cwd=repo_dir,
        )
        if rc != 0:
            logger.error("git commit failed: %s", out)
            return False

        # Push
        rc, out = await self._run_git(
            "push", "-u", "origin", branch,
            cwd=repo_dir,
        )
        if rc != 0:
            logger.error("git push failed: %s", out)
            return False

        return True

    async def create_pr(
        self,
        project_id: str,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> dict:
        """Create a pull request using the git provider API.

        Returns {"url": str, "number": int}.
        """
        project = await self.db.fetchrow(
            "SELECT repo_url, git_provider_id "
            "FROM projects WHERE id = $1",
            project_id,
        )
        if not project:
            raise ValueError(f"Project {project_id} not found")

        repo_url = project["repo_url"]
        git_provider_id = str(project["git_provider_id"]) if project.get("git_provider_id") else None

        # Resolve credentials and API base URL from git_provider_configs
        token = None
        provider_type = None
        api_base_url = None
        if git_provider_id:
            gp_row = await self.db.fetchrow(
                "SELECT provider_type, api_base_url, token_enc "
                "FROM git_provider_configs WHERE id = $1",
                git_provider_id,
            )
            if gp_row:
                token = decrypt(gp_row["token_enc"]) if gp_row.get("token_enc") else None
                provider_type = gp_row["provider_type"]
                api_base_url = gp_row.get("api_base_url")

        provider = create_git_provider(
            provider_type=provider_type,
            api_base_url=api_base_url,
            token=token,
            repo_url=repo_url,
        )

        owner, repo = parse_repo_url(project["repo_url"])
        if not owner or not repo:
            raise ValueError(f"Cannot parse repo URL: {project['repo_url']}")

        return await provider.create_pull_request(
            owner, repo,
            head=head_branch,
            base=base_branch,
            title=title,
            body=body,
        )

    async def create_pr_for_repo(
        self,
        *,
        repo_url: str,
        git_provider_id: str | None = None,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> dict:
        """Create a PR for any repo (not just the project's main repo).

        Like create_pr but takes explicit repo_url and git_provider_id
        instead of looking them up from the projects table.
        Returns {"url": str, "number": int}.
        """
        token = None
        provider_type = None
        api_base_url = None
        if git_provider_id:
            gp_row = await self.db.fetchrow(
                "SELECT provider_type, api_base_url, token_enc "
                "FROM git_provider_configs WHERE id = $1",
                git_provider_id,
            )
            if gp_row:
                token = decrypt(gp_row["token_enc"]) if gp_row.get("token_enc") else None
                provider_type = gp_row["provider_type"]
                api_base_url = gp_row.get("api_base_url")

        if not provider_type:
            provider_type = detect_provider_type(repo_url)

        provider = create_git_provider(
            provider_type=provider_type,
            api_base_url=api_base_url,
            token=token,
            repo_url=repo_url,
        )

        owner, repo = parse_repo_url(repo_url)
        if not owner or not repo:
            raise ValueError(f"Cannot parse repo URL: {repo_url}")

        return await provider.create_pull_request(
            owner, repo,
            head=head_branch,
            base=base_branch,
            title=title,
            body=body,
        )

    # ------------------------------------------------------------------
    # File tree
    # ------------------------------------------------------------------

    def get_file_tree(self, workspace_path: str, max_depth: int = 5) -> str:
        """Build a directory tree string for the workspace repo."""
        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        lines: list[str] = []
        skip_dirs = {
            ".git", "node_modules", "__pycache__", ".venv", "venv",
            "dist", "build", ".next", ".nuxt", "target", ".tox",
        }

        def _walk(path: str, prefix: str, depth: int) -> None:
            if depth > max_depth:
                lines.append(f"{prefix}...")
                return
            try:
                entries = sorted(os.listdir(path))
            except PermissionError:
                return
            dirs = [e for e in entries if os.path.isdir(os.path.join(path, e)) and e not in skip_dirs]
            files = [e for e in entries if os.path.isfile(os.path.join(path, e))]
            for f in files:
                lines.append(f"{prefix}{f}")
            for d in dirs:
                lines.append(f"{prefix}{d}/")
                _walk(os.path.join(path, d), prefix + "  ", depth + 1)

        _walk(repo_dir, "", 0)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _clone_or_pull(
        self,
        repo_url: str,
        target_dir: str,
        branch: str,
        token: str | None,
        provider_type: str | None,
    ) -> bool:
        """Clone if target doesn't exist, otherwise pull latest."""
        clone_url = build_clone_url(repo_url, token, provider_type)

        if os.path.isdir(os.path.join(target_dir, ".git")):
            # Pull latest
            rc, out = await self._run_git("fetch", "origin", cwd=target_dir)
            if rc != 0:
                logger.warning("git fetch failed for %s: %s", target_dir, out)
                return False
            rc, out = await self._run_git(
                "reset", "--hard", f"origin/{branch}" if branch != "HEAD" else "FETCH_HEAD",
                cwd=target_dir,
            )
            return rc == 0

        # Fresh clone
        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        args = ["clone", "--depth", "1"]
        if branch and branch != "HEAD":
            args.extend(["--branch", branch])
        args.extend([clone_url, target_dir])

        rc, out = await self._run_git(*args, cwd=self.workspace_root)
        if rc != 0:
            logger.error("git clone failed: %s", out)
            return False
        return True

    async def _run_git(self, *args: str, cwd: str) -> tuple[int, str]:
        """Run a git command asynchronously."""
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        stdout, _ = await proc.communicate()
        return proc.returncode or 0, stdout.decode(errors="replace")
