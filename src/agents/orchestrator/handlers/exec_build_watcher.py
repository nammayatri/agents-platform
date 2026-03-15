"""Execution handler — build watcher (monitor CI builds post-merge)."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from agents.orchestrator.handlers._base import HandlerContext

logger = logging.getLogger(__name__)


async def execute_build_watcher(
    ctx: HandlerContext, sub_task: dict, provider, workspace_path: str | None,
) -> None:
    """Procedural: monitor CI builds post-merge."""
    import httpx as _httpx

    from agents.orchestrator.handlers._shared import resolve_git_for_subtask

    st_id = str(sub_task["id"])
    await ctx.transition_subtask(st_id, "assigned")
    await ctx.transition_subtask(st_id, "running")

    await ctx.db.execute(
        "UPDATE todo_items SET sub_state = 'build_watching', updated_at = NOW() WHERE id = $1",
        ctx.todo_id,
    )

    try:
        todo = await ctx.load_todo()
        project = await ctx.db.fetchrow(
            "SELECT * FROM projects WHERE id = $1", todo["project_id"]
        )
        if not project or not project.get("repo_url"):
            raise ValueError("No repo configured for build watching")

        project_settings = project.get("settings_json") or {}
        if isinstance(project_settings, str):
            project_settings = json.loads(project_settings)
        release_config = project_settings.get("release_config", {})
        build_config = release_config.get("build", {})

        pr_deliv = await ctx.db.fetchrow(
            "SELECT * FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
            "AND pr_state = 'merged' ORDER BY created_at DESC LIMIT 1",
            ctx.todo_id,
        )
        if not pr_deliv:
            raise ValueError("No merged PR deliverable found for build watching")

        await ctx.notifier.notify(
            str(todo["creator_id"]),
            "build_started",
            {
                "todo_id": ctx.todo_id,
                "title": todo["title"],
                "detail": "Build pipeline started after merge.",
            },
        )

        await ctx.report_progress(st_id, 10, "Monitoring build pipeline")

        git, owner, repo = await resolve_git_for_subtask(ctx, sub_task, project)

        build_provider = build_config.get("provider", "github_actions")
        poll_interval = build_config.get("poll_interval_seconds", 30)
        timeout_minutes = build_config.get("timeout_minutes", 30)
        deadline = time.monotonic() + timeout_minutes * 60
        image_hash = None

        if build_provider == "github_actions":
            workflow_name = build_config.get("workflow_name")
            head_sha = pr_deliv.get("head_sha") or ""

            while time.monotonic() < deadline:
                resp = await git.http.get(
                    f"{git.api_base_url}/repos/{owner}/{repo}/actions/runs",
                    params={"event": "push", "per_page": "5"},
                )
                resp.raise_for_status()
                runs = resp.json().get("workflow_runs", [])

                if workflow_name:
                    runs = [r for r in runs if r.get("name") == workflow_name]

                target_run = None
                for run in runs:
                    if head_sha and run.get("head_sha") == head_sha:
                        target_run = run
                        break
                if not target_run and runs:
                    target_run = runs[0]

                if target_run:
                    status = target_run.get("status")
                    conclusion = target_run.get("conclusion")

                    if status == "completed":
                        if conclusion == "success":
                            run_id = target_run["id"]
                            try:
                                art_resp = await git.http.get(
                                    f"{git.api_base_url}/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
                                )
                                art_resp.raise_for_status()
                                artifacts = art_resp.json().get("artifacts", [])
                                for artifact in artifacts:
                                    name = (artifact.get("name") or "").lower()
                                    if "digest" in name or "hash" in name:
                                        image_hash = artifact.get("name")
                                        break
                            except Exception:
                                logger.debug("Could not fetch artifacts for run %s", run_id)

                            if not image_hash:
                                image_hash = target_run.get("head_sha", head_sha)

                            logger.info("[%s] Build succeeded, image_hash=%s", ctx.todo_id, image_hash)
                            break
                        else:
                            raise ValueError(
                                f"Build failed with conclusion '{conclusion}': {target_run.get('html_url', 'N/A')}"
                            )

                await ctx.report_progress(
                    st_id, 30, f"Build in progress... (polling every {poll_interval}s)"
                )
                await asyncio.sleep(poll_interval)
            else:
                raise ValueError(f"Build timed out after {timeout_minutes} minutes")

        elif build_provider == "jenkins":
            job_url = build_config.get("job_url", "").rstrip("/")
            jenkins_token = build_config.get("token")
            if not job_url:
                raise ValueError("Jenkins job_url not configured in build config")

            headers = {}
            if jenkins_token:
                headers["Authorization"] = f"Bearer {jenkins_token}"

            async with _httpx.AsyncClient(timeout=30, headers=headers) as client:
                resp = await client.get(f"{job_url}/api/json")
                resp.raise_for_status()
                initial_last = resp.json().get("lastCompletedBuild", {}).get("number", 0)

                while time.monotonic() < deadline:
                    resp = await client.get(f"{job_url}/api/json")
                    resp.raise_for_status()
                    data = resp.json()
                    last_completed = data.get("lastCompletedBuild", {}).get("number", 0)

                    if last_completed > initial_last:
                        build_resp = await client.get(
                            f"{job_url}/{last_completed}/api/json"
                        )
                        build_resp.raise_for_status()
                        build_data = build_resp.json()
                        result = build_data.get("result", "")

                        if result == "SUCCESS":
                            desc = build_data.get("description") or ""
                            if "sha256:" in desc:
                                image_hash = desc[desc.index("sha256:"):].split()[0]
                            else:
                                image_hash = str(last_completed)
                            logger.info("[%s] Jenkins build %d succeeded, hash=%s",
                                        ctx.todo_id, last_completed, image_hash)
                            break
                        else:
                            raise ValueError(
                                f"Jenkins build #{last_completed} failed with result: {result}"
                            )

                    await ctx.report_progress(
                        st_id, 30, f"Waiting for Jenkins build... (polling every {poll_interval}s)"
                    )
                    await asyncio.sleep(poll_interval)
                else:
                    raise ValueError(f"Jenkins build timed out after {timeout_minutes} minutes")

        else:
            raise ValueError(f"Unsupported build provider: {build_provider}")

        # Store artifact in deliverables
        artifact_data = {"image_hash": image_hash, "build_provider": build_provider}
        await ctx.db.execute(
            "UPDATE deliverables SET release_artifact_json = $2, head_sha = COALESCE(head_sha, $3) "
            "WHERE id = $1",
            pr_deliv["id"],
            artifact_data,
            image_hash,
        )

        await ctx.notifier.notify(
            str(todo["creator_id"]),
            "build_completed",
            {
                "todo_id": ctx.todo_id,
                "title": todo["title"],
                "detail": f"Build completed successfully. Artifact: {image_hash}",
            },
        )

        await ctx.report_progress(st_id, 100, "Build completed")
        await ctx.transition_subtask(
            st_id, "completed",
            progress_pct=100, progress_message=f"Build complete: {image_hash}",
            output_result={"image_hash": image_hash},
        )

    except Exception as e:
        logger.error("[%s] Build watcher failed: %s", ctx.todo_id, e, exc_info=True)
        try:
            todo = await ctx.load_todo()
            await ctx.notifier.notify(
                str(todo["creator_id"]),
                "build_failed",
                {
                    "todo_id": ctx.todo_id,
                    "title": todo["title"],
                    "detail": f"Build failed: {str(e)[:300]}",
                },
            )
        except Exception:
            pass
        await ctx.post_system_message(
            f"**Release pipeline:** Build failed: {str(e)[:500]}"
        )
        await ctx.transition_subtask(
            st_id, "failed",
            error_message=str(e)[:500],
        )
