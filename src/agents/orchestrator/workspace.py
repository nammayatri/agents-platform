"""Workspace manager for server-side repository cloning and task-level workspaces.

Directory structure:
    {WORKSPACE_ROOT}/
        {project_id}/
            repo/                   <- project-level reference clone (main repo)
            deps/
                {dep_name}/         <- project-level dep reference clones
            tasks/
                {todo_id}/          <- task_root
                    main/           <- writeable main repo clone (task branch)
                    {dep_name}/     <- writeable dep repo clone (task branch)
                    deps/           <- symlink → project_dir/deps/ (read-only)
                    .context/       <- context docs copy

workspace_path on sub_tasks = the git working directory itself.
    e.g. tasks/{todo_id}/main/ or tasks/{todo_id}/{dep_name}/

task_root = tasks/{todo_id}/
    Derived from workspace_path: os.path.dirname(workspace_path)

Agent's file access model (from inside main/):
    .              → repo code
    ../deps/{name}/ → read-only dep repos
    ../.context/    → project context docs
    ../{dep_name}/  → sibling writeable repo
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

# Reserved repo name for the main project repository
MAIN_REPO = "main"


def task_root_from_workspace(workspace_path: str) -> str:
    """Derive task_root from a workspace_path (git working dir).

    workspace_path = task_root/{name}/
    task_root = workspace_path/../
    """
    return os.path.normpath(os.path.join(workspace_path, ".."))


class WorkspaceManager:
    def __init__(self, db: asyncpg.Pool, workspace_root: str):
        self.db = db
        self.workspace_root = workspace_root
        os.makedirs(workspace_root, exist_ok=True)

    # ------------------------------------------------------------------
    # Project workspace (reference clones — unchanged)
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

        ok = await self._clone_or_pull(
            repo_url=repo_url,
            target_dir=repo_dir,
            branch=branch,
            token=token,
            provider_type=provider_type,
        )
        if not ok:
            raise RuntimeError(
                f"Failed to clone/pull project repo {repo_url} into {repo_dir}"
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
    # Task workspace setup
    # ------------------------------------------------------------------

    async def setup_task_workspace(self, todo_id: str) -> str:
        """Create the task_root and clone the main repo into task_root/main/.

        Returns task_root (tasks/{todo_id}/).
        The main repo workspace_path is task_root/main/.
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

        # Verify the project repo is a valid, functional git clone.
        repo_valid = False
        if os.path.isdir(project_repo_dir):
            rc, _ = await run_git_command(
                "rev-parse", "--git-dir", cwd=project_repo_dir,
            )
            repo_valid = rc == 0

        if not repo_valid:
            if os.path.isdir(project_repo_dir):
                logger.warning(
                    "[workspace] Project repo dir exists but is not a valid git repo, "
                    "removing and re-cloning for project=%s", project_id,
                )
                shutil.rmtree(project_repo_dir, ignore_errors=True)
            else:
                logger.info("[workspace] Project repo dir missing, re-cloning for project=%s", project_id)
            project_workspace = await self.setup_project_workspace(project_id)
            project_repo_dir = os.path.join(project_workspace, "repo")
            if not os.path.isdir(project_repo_dir):
                raise ValueError(f"Project repo not cloned at {project_repo_dir} after re-setup")

        short_id = str(todo_id)[:8]
        task_root = os.path.join(project_workspace, "tasks", str(todo_id))
        main_workspace = os.path.join(task_root, MAIN_REPO)

        if os.path.isdir(main_workspace):
            logger.info("[workspace] Task workspace exists, verifying for todo=%s", todo_id)
            await ensure_authenticated_remote(main_workspace, self.db)
            # Don't fetch — task workspace doesn't need remote updates.
            # Fetching on large repos hangs for minutes.
            branch_name = f"task/{short_id}"
            rc, current = await run_git_command(
                "rev-parse", "--abbrev-ref", "HEAD", cwd=main_workspace,
            )
            current = current.strip() if rc == 0 else ""
            if current != branch_name:
                logger.info("[workspace] Checking out task branch %s (was on %s)", branch_name, current)
                rc, _ = await run_git_command("checkout", branch_name, cwd=main_workspace)
                if rc != 0:
                    await run_git_command("checkout", "-b", branch_name, cwd=main_workspace)
            return task_root

        # ── Pull project repo to latest before cloning task workspace ──
        branch = todo.get("default_branch") or "main"
        await ensure_authenticated_remote(project_repo_dir, self.db)
        rc_pull, _ = await run_git_command(
            "pull", "--ff-only", "origin", branch, cwd=project_repo_dir,
        )
        if rc_pull == 0:
            logger.info("[workspace] Project repo fast-forwarded to origin/%s for todo=%s", branch, todo_id)
        else:
            logger.warning("[workspace] Project repo pull --ff-only failed for project=%s (non-fatal, using as-is)", project_id)

        os.makedirs(task_root, exist_ok=True)

        clone_url = todo.get("repo_url") or ""
        git_provider_id = str(todo["git_provider_id"]) if todo.get("git_provider_id") else None
        logger.info("[workspace] Cloning task workspace: todo=%s clone_url=%s", todo_id, clone_url[:50] if clone_url else "none")
        token, provider_type, _ = await resolve_git_credentials(
            self.db, git_provider_id, clone_url,
        )

        rc, out = await run_git_command(
            "clone", project_repo_dir, main_workspace,
            cwd=self.workspace_root,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to clone task workspace: {out}")

        authenticated_url = build_clone_url(clone_url, token, provider_type)
        await run_git_command(
            "remote", "set-url", "origin", authenticated_url,
            cwd=main_workspace,
        )

        # Record base commit for diff computation
        rc_base, base_hash = await run_git_command("rev-parse", "HEAD", cwd=main_workspace)
        if rc_base == 0:
            base_commit = base_hash.strip()
            await self.db.execute(
                "UPDATE todo_items SET base_commit = $2 WHERE id = $1",
                todo_id, base_commit,
            )
            logger.info("[workspace] Stored base_commit=%s for todo=%s", base_commit[:12], todo_id)

        branch_name = f"task/{short_id}"
        logger.info("[workspace] Created task branch=%s for todo=%s", branch_name, todo_id)
        await run_git_command("checkout", "-b", branch_name, cwd=main_workspace)

        # Copy project context docs into task_root/.context/
        project_context = os.path.join(project_workspace, ".context")
        task_context = os.path.join(task_root, ".context")
        if os.path.isdir(project_context) and not os.path.exists(task_context):
            try:
                shutil.copytree(project_context, task_context)
            except Exception:
                logger.debug("Could not copy .context/ into task workspace")

        # Symlink project deps into task_root/deps/ so ../deps/{name}/ works
        # from task_root/main/ (matches the path convention in tool descriptions).
        project_deps = os.path.join(project_workspace, "deps")
        task_deps_link = os.path.join(task_root, "deps")
        if os.path.isdir(project_deps) and not os.path.exists(task_deps_link):
            try:
                os.symlink(project_deps, task_deps_link)
            except Exception:
                logger.debug("Could not symlink deps into task root")

        return task_root

    # ------------------------------------------------------------------
    # Per-repo workspace setup (unified for main and deps)
    # ------------------------------------------------------------------

    async def setup_repo_workspace(
        self,
        todo_id: str,
        repo_name: str,
        repo_url: str,
        *,
        default_branch: str = "main",
        git_provider_id: str | None = None,
    ) -> str:
        """Set up a writeable workspace for any repo (main or dependency).

        Clones the repo into task_root/{repo_name}/ and creates a
        task branch. Returns the workspace_path (the git working directory).

        This is the unified method — main repo and dep repos use the same
        code path. For the main repo, setup_task_workspace calls this
        internally.
        """
        todo = await self.db.fetchrow(
            "SELECT t.*, p.workspace_path AS project_workspace, p.git_provider_id AS project_gp_id "
            "FROM todo_items t JOIN projects p ON t.project_id = p.id "
            "WHERE t.id = $1",
            todo_id,
        )
        if not todo:
            raise ValueError(f"Todo {todo_id} not found")

        project_workspace = todo.get("project_workspace")
        if not project_workspace or not os.path.isdir(project_workspace):
            project_workspace = await self.setup_project_workspace(str(todo["project_id"]))

        short_id = str(todo_id)[:8]
        task_root = os.path.join(project_workspace, "tasks", str(todo_id))
        workspace_path = os.path.join(task_root, repo_name)

        if os.path.isdir(workspace_path):
            logger.info("[workspace] Reusing existing repo workspace: %s", workspace_path)
            # Don't fetch — the repo was cloned for this task and doesn't need
            # updates. Fetching on large repos can hang for minutes downloading
            # full ref lists. Just ensure auth is valid and return.
            await ensure_authenticated_remote(workspace_path, self.db)
            return workspace_path

        logger.info("[workspace] Cloning repo '%s' → %s", repo_name, workspace_path)
        os.makedirs(os.path.dirname(workspace_path), exist_ok=True)

        # Resolve git credentials
        effective_gp_id = git_provider_id
        if not effective_gp_id:
            effective_gp_id = str(todo["project_gp_id"]) if todo.get("project_gp_id") else None

        token, provider_type, _ = await resolve_git_credentials(
            self.db, effective_gp_id, repo_url,
        )

        # Try local clone from project deps first (faster than remote)
        project_dep_dir = os.path.join(project_workspace, "deps", repo_name)
        if os.path.isdir(os.path.join(project_dep_dir, ".git")):
            # Pull latest on the project dep first
            await self._clone_or_pull(
                repo_url=repo_url,
                target_dir=project_dep_dir,
                branch="HEAD",
                token=token,
                provider_type=provider_type,
            )
            rc, out = await run_git_command(
                "clone", project_dep_dir, workspace_path,
                cwd=self.workspace_root,
            )
        else:
            # No local reference — clone from remote
            authenticated_url = build_clone_url(repo_url, token, provider_type)
            rc, out = await run_git_command(
                "clone", "--depth", "1", authenticated_url, workspace_path,
                cwd=self.workspace_root,
            )

        if rc != 0:
            raise RuntimeError(f"git clone failed for '{repo_name}': {out}")

        # Set authenticated remote
        authenticated_url = build_clone_url(repo_url, token, provider_type)
        await run_git_command(
            "remote", "set-url", "origin", authenticated_url,
            cwd=workspace_path,
        )

        # Create task branch
        branch_name = f"task/{short_id}-{repo_name}" if repo_name != MAIN_REPO else f"task/{short_id}"
        await run_git_command("checkout", "-b", branch_name, cwd=workspace_path)
        logger.info("[workspace] Created branch %s for repo '%s'", branch_name, repo_name)

        return workspace_path

    async def cleanup_task_workspace(self, todo_id: str) -> None:
        """Remove a task workspace after completion."""
        todo = await self.db.fetchrow(
            "SELECT project_id FROM todo_items WHERE id = $1", todo_id
        )
        if not todo:
            return

        project_dir = os.path.join(self.workspace_root, str(todo["project_id"]))
        task_root = os.path.join(project_dir, "tasks", str(todo_id))

        if os.path.isdir(task_root):
            shutil.rmtree(task_root, ignore_errors=True)
            logger.info("Cleaned up task workspace %s", task_root)

    async def evict_old_workspaces(self, max_total_bytes: int = 10 * 1024**3) -> int:
        """LRU-evict completed task workspaces when total size exceeds limit."""
        task_dirs: list[tuple[float, str, str]] = []  # (mtime, path, todo_id)

        for project_name in os.listdir(self.workspace_root):
            project_path = os.path.join(self.workspace_root, project_name)
            tasks_root = os.path.join(project_path, "tasks")
            if not os.path.isdir(tasks_root):
                continue
            for todo_name in os.listdir(tasks_root):
                task_path = os.path.join(tasks_root, todo_name)
                if not os.path.isdir(task_path):
                    continue
                try:
                    mtime = os.path.getmtime(task_path)
                except OSError:
                    continue
                task_dirs.append((mtime, task_path, todo_name))

        if not task_dirs:
            return 0

        total_bytes = 0
        dir_sizes: dict[str, int] = {}
        for _, path, _ in task_dirs:
            size = 0
            try:
                for dirpath, _dirnames, filenames in os.walk(path):
                    for f in filenames:
                        try:
                            size += os.path.getsize(os.path.join(dirpath, f))
                        except OSError:
                            pass
            except OSError:
                pass
            dir_sizes[path] = size
            total_bytes += size

        if total_bytes <= max_total_bytes:
            return 0

        task_dirs.sort(key=lambda t: t[0])

        terminal_ids: set[str] = set()
        try:
            rows = await self.db.fetch(
                "SELECT id FROM todo_items WHERE state IN ('completed', 'cancelled', 'failed')"
            )
            terminal_ids = {str(r["id"]) for r in rows}
        except Exception:
            logger.warning("Could not fetch terminal task IDs for eviction")
            return 0

        evicted = 0
        for _mtime, path, todo_id in task_dirs:
            if total_bytes <= max_total_bytes:
                break
            if todo_id not in terminal_ids:
                continue
            freed = dir_sizes.get(path, 0)
            try:
                shutil.rmtree(path, ignore_errors=True)
                total_bytes -= freed
                evicted += 1
                logger.info("Evicted workspace %s (freed ~%dMB)", path, freed // (1024 * 1024))
            except Exception:
                logger.debug("Failed to evict workspace %s", path)

        return evicted

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    async def run_command(
        self, cmd: str, cwd: str, *, timeout: int = 60,
    ) -> tuple[int, str]:
        """Run a shell command in the given directory."""
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
    ) -> dict:
        """Stage all changes, commit, and push to remote.

        workspace_path is the git working directory directly (no /repo/ subdir).

        Returns a dict with:
            success (bool): True if the branch was successfully pushed.
            error (str | None): Error description on failure.
            pre_commit_failed (bool): True if commit failed due to pre-commit hooks.
            pre_commit_output (str | None): Raw pre-commit/hook error output.
        """
        result: dict = {
            "success": False,
            "error": None,
            "pre_commit_failed": False,
            "pre_commit_output": None,
        }

        if not os.path.isdir(os.path.join(workspace_path, ".git")):
            logger.error("commit_and_push: no .git directory in %s", workspace_path)
            result["error"] = "no .git directory"
            return result

        await ensure_authenticated_remote(workspace_path, self.db)

        rc, current_branch = await run_git_command("rev-parse", "--abbrev-ref", "HEAD", cwd=workspace_path)
        if rc == 0:
            current_branch = current_branch.strip()
            if current_branch != branch:
                logger.info("Switching from %s to %s", current_branch, branch)
                rc, out = await run_git_command("checkout", branch, cwd=workspace_path)
                if rc != 0:
                    rc, out = await run_git_command("checkout", "-b", branch, cwd=workspace_path)
                    if rc != 0:
                        logger.error("git checkout -b %s failed: %s", branch, out)
                        result["error"] = f"checkout failed: {out}"
                        return result

        rc, out = await run_git_command("add", "-A", cwd=workspace_path)
        if rc != 0:
            logger.error("git add failed: %s", out)
            result["error"] = f"git add failed: {out}"
            return result

        rc, out = await run_git_command("diff", "--cached", "--quiet", cwd=workspace_path)
        if rc == 0:
            logger.info("No new changes to commit — checking if branch has unpushed commits")
        else:
            rc, out = await run_git_command("commit", "-m", message, cwd=workspace_path)
            if rc != 0:
                hook_indicators = [
                    "pre-commit", "hook", "husky", "lint-staged",
                    "eslint", "prettier", "tsc", "mypy", "ruff",
                    "flake8", "black", "isort",
                ]
                out_lower = out.lower()
                is_hook_failure = any(ind in out_lower for ind in hook_indicators)
                if is_hook_failure:
                    logger.warning("commit_and_push: pre-commit hook failed:\n%s", out[:2000])
                    result["pre_commit_failed"] = True
                    result["pre_commit_output"] = out
                    result["error"] = "pre-commit hook failed"
                else:
                    logger.error("git commit failed: %s", out)
                    result["error"] = f"git commit failed: {out}"
                return result
            logger.info("Committed changes on branch %s", branch)

        rc, out = await run_git_command("push", "-u", "origin", branch, cwd=workspace_path)
        if rc != 0:
            rc_s, is_shallow = await run_git_command(
                "rev-parse", "--is-shallow-repository", cwd=workspace_path,
            )
            if rc_s == 0 and is_shallow.strip() == "true":
                logger.info("Push failed on shallow clone, unshallowing and retrying branch %s", branch)
                await run_git_command("fetch", "--unshallow", "origin", cwd=workspace_path)
                rc, out = await run_git_command("push", "-u", "origin", branch, cwd=workspace_path)

            if rc != 0:
                logger.error("git push failed: %s", out)
                result["error"] = f"git push failed: {out}"
                return result

        logger.info("Pushed branch %s to origin", branch)
        result["success"] = True
        return result

    async def create_pr(
        self,
        project_id: str,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
    ) -> dict:
        """Create a pull request using the git provider API."""
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
        """Create a PR for any repo (not just the project's main repo)."""
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

    def get_file_tree(self, workspace_path: str, max_depth: int = 3, max_chars: int = 8000) -> str:
        """Build a directory tree string for the workspace.

        workspace_path IS the git working directory (no /repo/ subdir).
        Truncates to max_chars to avoid blowing token budgets on large repos.
        """
        tree = build_file_tree_text(workspace_path, max_depth)
        if len(tree) > max_chars:
            tree = tree[:max_chars] + f"\n... (truncated, {len(tree) - max_chars} more chars)"
        return tree

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
            rc, out = await run_git_command(
                "pull", "--ff-only", "origin",
                *([] if branch == "HEAD" else [branch]),
                cwd=target_dir,
            )
            if rc != 0:
                logger.warning("git pull --ff-only failed for %s: %s (using as-is)", target_dir, out[:200])
            return True

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
