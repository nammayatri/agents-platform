"""Workspace IDE endpoints: file browsing, editing, and git operations.

Extracted from todos.py to keep route modules focused.
Resolves the task workspace at {project_workspace}/tasks/{todo_id}/repo/
or {project_workspace}/tasks/{todo_id}/dep_{repo}/repo/ for dependency repos.
"""

import json
import os

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser, EventBusDep, Redis, check_project_access
from agents.orchestrator.state_machine import transition_subtask
from agents.utils.file_utils import (
    MAX_FILE_SIZE,
    build_file_tree,
    detect_language,
    is_binary,
    validate_workspace_path,
)
from agents.utils.git_utils import ensure_authenticated_remote, run_git_command

router = APIRouter()


# ── Helpers ──────────────────────────────────────────────────────────


async def _resolve_task_repo(
    todo_id: str, user, db, repo: str | None = None,
) -> tuple[str, str]:
    """Resolve the task workspace repo directory.

    Args:
        repo: Optional dependency repo name. When provided and not "main",
              resolves to dep_{repo}/repo/ instead of repo/.

    Returns (repo_dir, todo_id).
    """
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail="Task not found")
    await check_project_access(db, str(todo["project_id"]), user)
    project = await db.fetchrow(
        "SELECT workspace_path FROM projects WHERE id = $1",
        str(todo["project_id"]),
    )
    if not project or not project.get("workspace_path"):
        raise HTTPException(status_code=404, detail="No workspace configured")

    task_dir = os.path.join(str(project["workspace_path"]), "tasks", todo_id)
    if repo and repo != "main":
        dep_name = repo.replace("/", "_").replace(" ", "_")
        repo_dir = os.path.join(task_dir, f"dep_{dep_name}", "repo")
    else:
        repo_dir = os.path.join(task_dir, "repo")

    if not os.path.isdir(repo_dir):
        raise HTTPException(status_code=404, detail="Task workspace not found")
    return repo_dir, todo_id


def _raise_path_error(msg: str) -> None:
    """Raise an HTTPException for path validation errors."""
    raise HTTPException(status_code=400, detail=msg)


# ── Repo Listing ─────────────────────────────────────────────────────


@router.get("/todos/{todo_id}/workspace/repos")
async def workspace_repos(todo_id: str, user: CurrentUser, db: DB):
    """List available repos (main + dependency workspaces) for a task."""
    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail="Task not found")
    await check_project_access(db, str(todo["project_id"]), user)
    project = await db.fetchrow(
        "SELECT workspace_path FROM projects WHERE id = $1",
        str(todo["project_id"]),
    )
    if not project or not project.get("workspace_path"):
        raise HTTPException(status_code=404, detail="No workspace configured")

    task_dir = os.path.join(str(project["workspace_path"]), "tasks", todo_id)
    repos = [{"name": "main", "label": "Main Repository"}]

    if os.path.isdir(task_dir):
        for entry in sorted(os.listdir(task_dir)):
            if entry.startswith("dep_") and os.path.isdir(
                os.path.join(task_dir, entry, "repo")
            ):
                dep_name = entry[4:]  # strip "dep_" prefix
                repos.append({"name": dep_name, "label": dep_name})

    return repos


# ── File Tree & File Read/Write ──────────────────────────────────────


@router.get("/todos/{todo_id}/workspace/tree")
async def workspace_tree(
    todo_id: str, user: CurrentUser, db: DB,
    repo: str = Query(None, description="Repo name: 'main' or dependency name"),
):
    """Get the file tree for a task workspace."""
    repo_dir, _ = await _resolve_task_repo(todo_id, user, db, repo=repo)
    tree = build_file_tree(repo_dir, repo_dir)
    return tree


@router.get("/todos/{todo_id}/workspace/file")
async def workspace_read_file(
    todo_id: str, user: CurrentUser, db: DB,
    path: str = Query(..., description="Relative file path"),
    repo: str = Query(None, description="Repo name: 'main' or dependency name"),
):
    """Read a file from the task workspace."""
    repo_dir, _ = await _resolve_task_repo(todo_id, user, db, repo=repo)
    try:
        full_path = validate_workspace_path(repo_dir, path)
    except ValueError as e:
        _raise_path_error(str(e))

    if not os.path.isfile(full_path):
        raise HTTPException(status_code=404, detail="File not found")

    size = os.path.getsize(full_path)
    language = detect_language(path)
    binary = is_binary(path)

    if binary:
        return {
            "path": path,
            "content": "",
            "size": size,
            "language": language,
            "binary": True,
        }

    truncated = size > MAX_FILE_SIZE
    try:
        with open(full_path, "r", errors="replace") as f:
            content = f.read(MAX_FILE_SIZE)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to read file")

    return {
        "path": path,
        "content": content,
        "size": size,
        "language": language,
        "binary": False,
        "truncated": truncated,
    }


class SaveFileInput(BaseModel):
    path: str
    content: str
    repo: str | None = None


@router.put("/todos/{todo_id}/workspace/file")
async def workspace_save_file(todo_id: str, body: SaveFileInput, user: CurrentUser, db: DB):
    """Save a file to the task workspace."""
    repo_dir, _ = await _resolve_task_repo(todo_id, user, db, repo=body.repo)
    try:
        full_path = validate_workspace_path(repo_dir, body.path)
    except ValueError as e:
        _raise_path_error(str(e))

    os.makedirs(os.path.dirname(full_path), exist_ok=True)

    try:
        with open(full_path, "w") as f:
            f.write(body.content)
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to save file")

    return {
        "path": body.path,
        "size": len(body.content.encode("utf-8")),
        "saved": True,
    }


# ── Git Operations ───────────────────────────────────────────────────


@router.get("/todos/{todo_id}/workspace/git/status")
async def workspace_git_status(
    todo_id: str, user: CurrentUser, db: DB,
    repo: str = Query(None, description="Repo name: 'main' or dependency name"),
):
    """Get git status for the task workspace."""
    repo_dir, _ = await _resolve_task_repo(todo_id, user, db, repo=repo)

    rc, branch = await run_git_command("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir)
    branch = branch.strip() if rc == 0 else "unknown"

    rc, output = await run_git_command("status", "--porcelain", cwd=repo_dir)
    files = []
    if rc == 0 and output.strip():
        for line in output.strip().split("\n"):
            if len(line) < 4:
                continue
            x = line[0]  # index status
            y = line[1]  # worktree status
            file_path = line[3:]
            if " -> " in file_path:
                file_path = file_path.split(" -> ")[-1]

            staged = x != " " and x != "?"
            if x == "?" and y == "?":
                file_status = "??"
            elif staged:
                file_status = x
            else:
                file_status = y

            files.append({
                "path": file_path,
                "status": file_status,
                "staged": staged,
            })

    return {
        "branch": branch,
        "files": files,
        "clean": len(files) == 0,
    }


@router.get("/todos/{todo_id}/workspace/git/diff")
async def workspace_git_diff(
    todo_id: str, user: CurrentUser, db: DB,
    staged: bool = Query(False, description="Show staged diff"),
    path: str = Query(None, description="Optional file path for per-file diff"),
    repo: str = Query(None, description="Repo name: 'main' or dependency name"),
):
    """Get git diff for the task workspace.

    When `path` is provided, returns the diff for that single file only.
    For untracked files (`??` status), returns a synthetic diff showing the
    full file content as additions.
    """
    repo_dir, _ = await _resolve_task_repo(todo_id, user, db, repo=repo)

    args = ["diff"]
    if staged:
        args.append("--cached")

    if path:
        # Validate the path doesn't escape the workspace
        if ".." in path:
            raise HTTPException(status_code=400, detail="Path traversal not allowed")
        args.append("--")
        args.append(path)

    rc, diff = await run_git_command(*args, cwd=repo_dir)
    _, stats = await run_git_command(*args, "--stat", cwd=repo_dir)

    # For untracked files, git diff returns nothing — synthesize a diff
    if path and rc == 0 and not diff.strip():
        full_path = os.path.join(repo_dir, path)
        if os.path.isfile(full_path) and not is_binary(path):
            try:
                with open(full_path, "r", errors="replace") as f:
                    content = f.read(MAX_FILE_SIZE)
                lines = content.split("\n")
                # Build a synthetic unified diff
                diff_lines = [
                    f"diff --git a/{path} b/{path}",
                    "new file mode 100644",
                    "--- /dev/null",
                    f"+++ b/{path}",
                    f"@@ -0,0 +1,{len(lines)} @@",
                ]
                for line in lines:
                    diff_lines.append(f"+{line}")
                diff = "\n".join(diff_lines)
                stats = f" {path} | {len(lines)} {'+'*min(len(lines), 40)}"
            except Exception:
                pass

    return {
        "diff": diff if rc == 0 else "",
        "stats": stats.strip() if rc == 0 else "",
    }


class GitAddInput(BaseModel):
    paths: list[str]
    repo: str | None = None


@router.post("/todos/{todo_id}/workspace/git/add")
async def workspace_git_add(todo_id: str, body: GitAddInput, user: CurrentUser, db: DB):
    """Stage files in the task workspace."""
    repo_dir, _ = await _resolve_task_repo(todo_id, user, db, repo=body.repo)

    for p in body.paths:
        if ".." in p and p != ".":
            raise HTTPException(status_code=400, detail="Path traversal not allowed")

    rc, output = await run_git_command("add", *body.paths, cwd=repo_dir)
    if rc != 0:
        raise HTTPException(status_code=400, detail=f"git add failed: {output}")

    return await workspace_git_status(todo_id, user, db, repo=body.repo)


class GitCommitInput(BaseModel):
    message: str
    repo: str | None = None


@router.post("/todos/{todo_id}/workspace/git/commit")
async def workspace_git_commit(
    todo_id: str, body: GitCommitInput, user: CurrentUser, db: DB, redis: Redis,
):
    """Commit staged changes in the task workspace. Notifies the orchestrator."""
    repo_dir, _ = await _resolve_task_repo(todo_id, user, db, repo=body.repo)

    rc, output = await run_git_command("commit", "-m", body.message, cwd=repo_dir)
    if rc != 0:
        raise HTTPException(status_code=400, detail=f"git commit failed: {output}")

    rc2, hash_out = await run_git_command("rev-parse", "--short", "HEAD", cwd=repo_dir)
    commit_hash = hash_out.strip() if rc2 == 0 else "unknown"

    # ── Orchestrator notification ──
    # 1. Cancel running/assigned sub-tasks
    running_subs = await db.fetch(
        "SELECT id, status FROM sub_tasks WHERE todo_id = $1 AND status IN ('running', 'assigned')",
        todo_id,
    )
    for sub in running_subs:
        try:
            await transition_subtask(
                db, str(sub["id"]), "cancelled",
                error_message="Cancelled: user made manual workspace edits",
                redis=redis,
            )
        except ValueError:
            pass

    # 2. Push chat message for coordinator
    await redis.rpush(
        f"task:{todo_id}:chat_input",
        f"[SYSTEM] User manually committed changes in workspace: {body.message}. "
        "Running sub-tasks have been cancelled. Review the changes and re-plan if needed.",
    )

    # 3. Set sub_state so orchestrator pauses until user resumes
    await db.execute(
        "UPDATE todo_items SET sub_state = 'workspace_edited', updated_at = NOW() WHERE id = $1",
        todo_id,
    )

    # 4. Publish WebSocket events
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({
            "type": "state_change",
            "state": "in_progress",
            "sub_state": "workspace_edited",
        }),
    )
    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "workspace_commit", "message": body.message, "hash": commit_hash}),
    )

    return {
        "hash": commit_hash,
        "message": body.message,
        "success": True,
    }


class GitPushInput(BaseModel):
    repo: str | None = None


@router.post("/todos/{todo_id}/workspace/git/push")
async def workspace_git_push(
    todo_id: str, user: CurrentUser, db: DB, redis: Redis,
    body: GitPushInput | None = None,
):
    """Push commits to remote in the task workspace."""
    repo_name = body.repo if body else None
    repo_dir, _ = await _resolve_task_repo(todo_id, user, db, repo=repo_name)

    # Look up project_id for credential resolution
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    project_id = str(todo["project_id"]) if todo else None

    await ensure_authenticated_remote(repo_dir, db, project_id=project_id)

    rc, branch = await run_git_command("rev-parse", "--abbrev-ref", "HEAD", cwd=repo_dir)
    branch = branch.strip() if rc == 0 else "main"

    rc, output = await run_git_command("push", "-u", "origin", branch, cwd=repo_dir)
    if rc != 0:
        raise HTTPException(status_code=400, detail=f"git push failed: {output}")

    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({"type": "workspace_push", "branch": branch}),
    )
    await redis.rpush(
        f"task:{todo_id}:chat_input",
        f"[SYSTEM] User pushed changes to remote on branch '{branch}'. "
        "The PR may have new commits.",
    )

    return {
        "success": True,
        "output": output.strip(),
        "branch": branch,
    }


# ── Resume Endpoint ──────────────────────────────────────────────────


@router.post("/todos/{todo_id}/resume")
async def resume_todo(
    todo_id: str, user: CurrentUser, db: DB, redis: Redis, event_bus: EventBusDep,
):
    """Resume a task paused due to user workspace edits.

    Clears the 'workspace_edited' sub_state and publishes an event to wake
    the orchestrator.
    """
    from agents.orchestrator.events import TaskEvent

    todo = await db.fetchrow("SELECT * FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404, detail="Task not found")
    await check_project_access(db, str(todo["project_id"]), user)

    if todo["state"] != "in_progress":
        raise HTTPException(status_code=400, detail="Task is not in progress")

    await db.execute(
        "UPDATE todo_items SET sub_state = 'executing', updated_at = NOW() WHERE id = $1",
        todo_id,
    )

    await event_bus.publish(TaskEvent(
        event_type="state_changed",
        todo_id=todo_id,
        state="in_progress",
        sub_state="executing",
        metadata={"resumed_after": "workspace_edit"},
    ))

    await redis.publish(
        f"task:{todo_id}:events",
        json.dumps({
            "type": "state_change",
            "state": "in_progress",
            "sub_state": "executing",
        }),
    )

    return {"status": "resumed"}
