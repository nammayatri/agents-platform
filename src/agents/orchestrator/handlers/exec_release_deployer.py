"""Execution handler — release deployer (trigger deployment via HTTP)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from agents.orchestrator.handlers._base import HandlerContext

logger = logging.getLogger(__name__)


def _interpolate_template(template: str, variables: dict[str, str]) -> str:
    """Replace {{key}} placeholders in template strings with variable values."""
    def replacer(match):
        key = match.group(1).strip()
        return variables.get(key, match.group(0))
    return re.sub(r'\{\{(\w+)\}\}', replacer, template)


async def execute_release_deployer(
    ctx: HandlerContext, sub_task: dict, provider, workspace_path: str | None,
) -> None:
    """Procedural: trigger deployment via HTTP."""
    import httpx as _httpx

    st_id = str(sub_task["id"])
    await ctx.transition_subtask(st_id, "assigned")
    await ctx.transition_subtask(st_id, "running")

    description = (sub_task.get("description") or "").lower()
    is_prod = "prod" in description
    env_name = "prod" if is_prod else "test"

    await ctx.db.execute(
        "UPDATE todo_items SET sub_state = $2, updated_at = NOW() WHERE id = $1",
        ctx.todo_id,
        f"releasing_{env_name}",
    )

    try:
        todo = await ctx.load_todo()
        project = await ctx.db.fetchrow(
            "SELECT * FROM projects WHERE id = $1", todo["project_id"]
        )
        if not project:
            raise ValueError("Project not found")

        project_settings = project.get("settings_json") or {}
        if isinstance(project_settings, str):
            project_settings = json.loads(project_settings)
        release_config = project_settings.get("release_config", {})

        config_key = "prod_release" if is_prod else "test_release"
        env_config = release_config.get(config_key, {})
        if not env_config.get("enabled"):
            logger.info("[%s] Release %s not enabled, skipping", ctx.todo_id, env_name)
            await ctx.transition_subtask(
                st_id, "completed",
                progress_pct=100, progress_message=f"{env_name} release skipped (not enabled)",
            )
            return

        # Approval gate for production
        if is_prod and env_config.get("require_approval"):
            current_sub_state = todo.get("sub_state")
            if current_sub_state != "release_prod_approved":
                await ctx.db.execute(
                    "UPDATE todo_items SET sub_state = 'awaiting_release_approval', updated_at = NOW() WHERE id = $1",
                    ctx.todo_id,
                )
                await ctx.post_system_message(
                    "**Release pipeline:** Production deployment is ready. Awaiting your approval to deploy to production."
                )
                await ctx.transition_subtask(
                    st_id, "pending",
                    progress_message="Awaiting production release approval",
                )
                await ctx.redis.publish(
                    f"task:{ctx.todo_id}:events",
                    json.dumps({
                        "type": "state_change",
                        "state": "in_progress",
                        "sub_state": "awaiting_release_approval",
                    }),
                )
                return
            else:
                await ctx.db.execute(
                    "UPDATE todo_items SET sub_state = 'releasing_prod', updated_at = NOW() WHERE id = $1",
                    ctx.todo_id,
                )

        await ctx.report_progress(st_id, 20, f"Preparing {env_name} deployment")

        # Get image_hash from build_watcher's output_result
        build_st = await ctx.db.fetchrow(
            "SELECT output_result FROM sub_tasks WHERE todo_id = $1 "
            "AND agent_role = 'release_build_watcher' AND status = 'completed' "
            "ORDER BY created_at DESC LIMIT 1",
            ctx.todo_id,
        )
        if not build_st:
            raise ValueError("No completed build_watcher subtask found")

        build_output = build_st.get("output_result") or {}
        if isinstance(build_output, str):
            build_output = json.loads(build_output)
        image_hash = build_output.get("image_hash", "")

        pr_deliv = await ctx.db.fetchrow(
            "SELECT head_sha FROM deliverables WHERE todo_id = $1 AND type = 'pull_request' "
            "ORDER BY created_at DESC LIMIT 1",
            ctx.todo_id,
        )
        commit_sha = (pr_deliv["head_sha"] if pr_deliv and pr_deliv.get("head_sha") else "")

        project_name = project.get("name", "")
        variables = {
            "image_hash": image_hash,
            "commit_sha": commit_sha,
            "env": env_name,
            "project_name": project_name,
            "todo_id": ctx.todo_id,
        }

        api_url = _interpolate_template(env_config.get("api_url", ""), variables)
        method = env_config.get("http_method", "POST").upper()

        raw_headers = env_config.get("headers", {})
        headers = {}
        for k, v in raw_headers.items():
            headers[k] = _interpolate_template(str(v), variables)

        body_template = env_config.get("body_template", "")
        body_str = _interpolate_template(body_template, variables)

        success_codes = env_config.get("success_status_codes", [200, 201, 202])

        await ctx.report_progress(st_id, 50, f"Triggering {env_name} deployment")

        async with _httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(
                method=method,
                url=api_url,
                headers=headers,
                content=body_str,
            )

            if resp.status_code not in success_codes:
                raise ValueError(
                    f"Deployment request failed with status {resp.status_code}: {resp.text[:500]}"
                )

            deploy_response = {}
            try:
                deploy_response = resp.json()
            except Exception:
                deploy_response = {"raw": resp.text[:1000]}

            logger.info("[%s] %s deployment triggered successfully: %s",
                        ctx.todo_id, env_name, resp.status_code)

            # Poll status URL if configured
            poll_url = env_config.get("poll_status_url")
            if poll_url:
                release_id = str(deploy_response.get("release_id", deploy_response.get("id", "")))
                poll_variables = {**variables, "release_id": release_id}
                resolved_poll_url = _interpolate_template(poll_url, poll_variables)
                poll_success = env_config.get("poll_success_value", "succeeded")
                poll_interval_secs = env_config.get("poll_interval_seconds", 15)
                poll_timeout = env_config.get("poll_timeout_minutes", 15)
                poll_deadline = time.monotonic() + poll_timeout * 60

                await ctx.report_progress(st_id, 70, f"Waiting for {env_name} deployment to complete")

                while time.monotonic() < poll_deadline:
                    poll_resp = await client.get(resolved_poll_url, headers=headers)
                    if poll_resp.status_code == 200:
                        try:
                            poll_data = poll_resp.json()
                            status_val = str(poll_data.get("status", "")).lower()
                            if status_val == poll_success.lower():
                                logger.info("[%s] %s deployment poll: succeeded", ctx.todo_id, env_name)
                                break
                            if status_val in ("failed", "error", "cancelled"):
                                raise ValueError(
                                    f"Deployment failed during polling: status={status_val}"
                                )
                        except ValueError:
                            raise
                        except Exception:
                            pass
                    await asyncio.sleep(poll_interval_secs)
                else:
                    raise ValueError(f"Deployment status poll timed out after {poll_timeout} minutes")

        await ctx.notifier.notify(
            str(todo["creator_id"]),
            f"release_{env_name}_completed",
            {
                "todo_id": ctx.todo_id,
                "title": todo["title"],
                "detail": f"Successfully deployed to {env_name}. Image: {image_hash}",
            },
        )

        await ctx.report_progress(st_id, 100, f"{env_name} deployment complete")
        await ctx.transition_subtask(
            st_id, "completed",
            progress_pct=100, progress_message=f"Deployed to {env_name}",
        )

    except Exception as e:
        logger.error("[%s] Release deployer (%s) failed: %s", ctx.todo_id, env_name, e, exc_info=True)
        try:
            todo = await ctx.load_todo()
            await ctx.notifier.notify(
                str(todo["creator_id"]),
                f"release_{env_name}_failed",
                {
                    "todo_id": ctx.todo_id,
                    "title": todo["title"],
                    "detail": f"Deployment to {env_name} failed: {str(e)[:300]}",
                },
            )
        except Exception:
            pass
        await ctx.post_system_message(
            f"**Release pipeline:** {env_name} deployment failed: {str(e)[:500]}"
        )
        await ctx.transition_subtask(
            st_id, "failed",
            error_message=str(e)[:500],
        )
