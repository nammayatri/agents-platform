import asyncio
import json
import os

from fastapi import APIRouter, HTTPException

from agents.api.deps import DB, CurrentUser, check_project_access

router = APIRouter()


@router.get("/todos/{todo_id}/deliverables")
async def list_deliverables(todo_id: str, user: CurrentUser, db: DB):
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    rows = await db.fetch(
        "SELECT * FROM deliverables WHERE todo_id = $1 ORDER BY created_at DESC",
        todo_id,
    )
    return [dict(r) for r in rows]


@router.get("/deliverables/{deliverable_id}")
async def get_deliverable(deliverable_id: str, user: CurrentUser, db: DB):
    row = await db.fetchrow("SELECT * FROM deliverables WHERE id = $1", deliverable_id)
    if not row:
        raise HTTPException(status_code=404)
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", row["todo_id"])
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)
    return dict(row)


@router.get("/deliverables/{deliverable_id}/diff")
async def get_deliverable_diff(deliverable_id: str, user: CurrentUser, db: DB):
    """Get git diff for a code_diff deliverable."""
    row = await db.fetchrow("SELECT * FROM deliverables WHERE id = $1", deliverable_id)
    if not row:
        raise HTTPException(status_code=404)
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", row["todo_id"])
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    # Return cached diff if available
    if row.get("content_json"):
        cj = row["content_json"]
        if isinstance(cj, str):
            cj = json.loads(cj)
        if cj.get("diff"):
            return cj

    # Try to fetch from workspace
    project = await db.fetchrow("SELECT workspace_path FROM projects WHERE id = $1", todo["project_id"])
    if not project or not project.get("workspace_path"):
        raise HTTPException(status_code=404, detail="No workspace available")

    task_workspace = os.path.join(project["workspace_path"], "tasks", str(row["todo_id"]))
    repo_dir = os.path.join(task_workspace, "repo")
    if not os.path.isdir(repo_dir):
        raise HTTPException(status_code=404, detail="Workspace not found")

    async def run_git(*args):
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return proc.returncode, stdout.decode(errors="replace")

    rc, diff_output = await run_git("diff", "HEAD~1", "HEAD")
    if rc != 0 or not diff_output.strip():
        raise HTTPException(status_code=404, detail="No diff available")

    _, stat_output = await run_git("diff", "--stat", "HEAD~1", "HEAD")
    _, files_output = await run_git("diff", "--name-status", "HEAD~1", "HEAD")
    files = []
    for line in files_output.strip().split("\n"):
        if line.strip():
            parts = line.split("\t", 1)
            if len(parts) == 2:
                files.append({"status": parts[0], "path": parts[1]})

    result = {
        "diff": diff_output[:500_000],
        "stats": stat_output.strip(),
        "files": files,
    }

    # Cache it for next time
    await db.execute(
        "UPDATE deliverables SET content_json = $2::jsonb WHERE id = $1",
        deliverable_id,
        json.dumps(result),
    )

    return result


@router.get("/todos/{todo_id}/runs")
async def list_agent_runs(todo_id: str, user: CurrentUser, db: DB):
    todo = await db.fetchrow("SELECT project_id FROM todo_items WHERE id = $1", todo_id)
    if not todo:
        raise HTTPException(status_code=404)
    await check_project_access(db, str(todo["project_id"]), user)

    rows = await db.fetch(
        "SELECT * FROM agent_runs WHERE todo_id = $1 ORDER BY started_at DESC",
        todo_id,
    )
    return [dict(r) for r in rows]
