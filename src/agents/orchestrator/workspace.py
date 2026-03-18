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

from agents.orchestrator.git_providers.factory import (
    build_clone_url,
    create_git_provider,
    parse_repo_url,
)
from agents.utils.file_utils import build_file_tree_text
from agents.utils.git_utils import (
    ensure_authenticated_remote,
    resolve_git_credentials,
    run_git_command,
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

        project_git_provider_id = str(project["git_provider_id"]) if project.get("git_provider_id") else None
        token, provider_type, _ = await resolve_git_credentials(
            self.db, project_git_provider_id, repo_url,
        )
        branch = project.get("default_branch") or "main"

        await self._clone_or_pull(
            repo_url=repo_url,
            target_dir=repo_dir,
            branch=branch,
            token=token,
            provider_type=provider_type,
        )

        context_docs = project.get("context_docs") or []
        if isinstance(context_docs, str):
            context_docs = json.loads(context_docs)

        async def _clone_dep(dep: dict) -> None:
            dep_url = dep.get("repo_url")
            if not dep_url:
                return
            dep_name = dep.get("name", "").replace("/", "_").replace(" ", "_") or "dep"
            dep_dir = os.path.join(deps_dir, dep_name)
            try:
                from agents.orchestrator.git_providers.factory import detect_provider_type

                dep_git_provider_id = dep.get("git_provider_id")
                dep_token, dep_provider_type, _ = await resolve_git_credentials(
                    self.db, dep_git_provider_id, dep_url,
                )
                if not dep_token and token:
                    dep_detected = detect_provider_type(dep_url)
                    if dep_detected == provider_type:
                        dep_token, dep_provider_type = token, provider_type

                await self._clone_or_pull(
                    repo_url=dep_url,
                    target_dir=dep_dir,
                    branch="HEAD",
                    token=dep_token,
                    provider_type=dep_provider_type,
                )
            except Exception:
                logger.warning("Failed to clone dependency %s from %s", dep_name, dep_url)

        dep_tasks = [_clone_dep(dep) for dep in context_docs]
        if dep_tasks:
            await asyncio.gather(*dep_tasks, return_exceptions=True)

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

        if not project_workspace or not os.path.isdir(project_workspace):
            logger.info("[workspace] No project workspace, setting up for project=%s", project_id)
            project_workspace = await self.setup_project_workspace(project_id)

        project_repo_dir = os.path.join(project_workspace, "repo")
        if not os.path.isdir(project_repo_dir):
            raise ValueError(f"Project repo not cloned at {project_repo_dir}")

        short_id = str(todo_id)[:8]
        task_dir = os.path.join(project_workspace, "tasks", str(todo_id))
        task_repo_dir = os.path.join(task_dir, "repo")

        if os.path.isdir(task_repo_dir):
            logger.info("[workspace] Task workspace exists, fetching latest for todo=%s", todo_id)
            await run_git_command("fetch", "origin", cwd=task_repo_dir)
            return task_dir

        os.makedirs(task_dir, exist_ok=True)

        clone_url = todo.get("repo_url") or ""
        git_provider_id = str(todo["git_provider_id"]) if todo.get("git_provider_id") else None
        logger.info("[workspace] Cloning task workspace: todo=%s clone_url=%s", todo_id, clone_url[:50] if clone_url else "none")
        token, provider_type, _ = await resolve_git_credentials(
            self.db, git_provider_id, clone_url,
        )

        rc, out = await run_git_command(
            "clone", project_repo_dir, task_repo_dir,
            cwd=self.workspace_root,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to clone task workspace: {out}")

        authenticated_url = build_clone_url(clone_url, token, provider_type)
        await run_git_command(
            "remote", "set-url", "origin", authenticated_url,
            cwd=task_repo_dir,
        )

        branch_name = f"task/{short_id}"
        logger.info("[workspace] Created task branch=%s for todo=%s", branch_name, todo_id)
        await run_git_command("checkout", "-b", branch_name, cwd=task_repo_dir)

        project_deps = os.path.join(project_workspace, "deps")
        task_deps = os.path.join(task_dir, "deps")
        if os.path.isdir(project_deps) and not os.path.exists(task_deps):
            os.symlink(project_deps, task_deps)

        # Copy project context docs (.context/) into task workspace
        project_context = os.path.join(project_workspace, ".context")
        task_context = os.path.join(task_dir, ".context")
        if os.path.isdir(project_context) and not os.path.exists(task_context):
            try:
                shutil.copytree(project_context, task_context)
            except Exception:
                logger.debug("Could not copy .context/ into task workspace")

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
        """Stage all changes, commit, and push to remote.

        Returns True if the branch was successfully pushed.
        """
        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            repo_dir = workspace_path

        if not os.path.isdir(os.path.join(repo_dir, ".git")):
            logger.error("commit_and_push: no .git directory in %s", repo_dir)
            return False

        await ensure_authenticated_remote(repo_dir, self.db)

        rc, current_branch = await run_git_command("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir)
        if rc == 0:
            current_branch = current_branch.strip()
            if current_branch != branch:
                logger.info("Switching from %s to %s", current_branch, branch)
                rc, out = await run_git_command("checkout", branch, cwd=repo_dir)
                if rc != 0:
                    rc, out = await run_git_command("checkout", "-b", branch, cwd=repo_dir)
                    if rc != 0:
                        logger.error("git checkout -b %s failed: %s", branch, out)
                        return False

        rc, out = await run_git_command("add", "-A", cwd=repo_dir)
        if rc != 0:
            logger.error("git add failed: %s", out)
            return False

        rc, out = await run_git_command("diff", "--cached", "--quiet", cwd=repo_dir)
        if rc == 0:
            logger.info("No new changes to commit — checking if branch has unpushed commits")
        else:
            rc, out = await run_git_command("commit", "-m", message, cwd=repo_dir)
            if rc != 0:
                logger.error("git commit failed: %s", out)
                return False
            logger.info("Committed changes on branch %s", branch)

        rc, out = await run_git_command("push", "-u", "origin", branch, cwd=repo_dir)
        if rc != 0:
            logger.error("git push failed: %s", out)
            return False

        logger.info("Pushed branch %s to origin", branch)
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

        token, provider_type, api_base_url = await resolve_git_credentials(
            self.db, git_provider_id, repo_url,
        )

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

        Returns {"url": str, "number": int}.
        """
        token, provider_type, api_base_url = await resolve_git_credentials(
            self.db, git_provider_id, repo_url,
        )

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
        return build_file_tree_text(repo_dir, max_depth)

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
            rc, out = await run_git_command("fetch", "origin", cwd=target_dir)
            if rc != 0:
                logger.warning("git fetch failed for %s: %s", target_dir, out)
                return False
            rc, out = await run_git_command(
                "reset", "--hard", f"origin/{branch}" if branch != "HEAD" else "FETCH_HEAD",
                cwd=target_dir,
            )
            return rc == 0

        os.makedirs(os.path.dirname(target_dir), exist_ok=True)
        args = ["clone", "--depth", "1"]
        if branch and branch != "HEAD":
            args.extend(["--branch", branch])
        args.extend([clone_url, target_dir])

        rc, out = await run_git_command(*args, cwd=self.workspace_root)
        if rc != 0:
            logger.error("git clone failed: %s", out)
            return False
        return True
