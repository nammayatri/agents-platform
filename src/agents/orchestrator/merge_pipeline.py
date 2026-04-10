"""Merge Pipeline Executor — standalone pipeline triggered by PR merges.

Runs independently of the task/todo system. Handles:
1. Test phase: poll an API or wait for webhook callback
2. Deploy phase: HTTP call or kubectl commands
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import shlex
import time

logger = logging.getLogger(__name__)

# Available template variables (shown in dashboard for users to pick from)
AVAILABLE_VARIABLES = [
    "commit_hash",
    "branch_name",
    "pr_number",
    "pr_title",
    "repo_url",
    "project_name",
]


def interpolate(template: str, variables: dict[str, str]) -> str:
    """Replace {{key}} placeholders in template strings."""
    def replacer(match: re.Match) -> str:
        key = match.group(1).strip()
        return variables.get(key, match.group(0))
    return re.sub(r'\{\{(\w+)\}\}', replacer, template)


def _match_repo_name(repo_url: str, project_repo_url: str, context_docs: list) -> str | None:
    """Match an incoming repo URL to a repo name ('main' or dep name).

    Normalises URLs by stripping trailing slashes and .git suffixes for comparison.
    """
    def _norm(url: str) -> str:
        return url.rstrip("/").removesuffix(".git").lower()

    incoming = _norm(repo_url)
    if not incoming:
        return "main"  # default when URL unknown

    if project_repo_url and _norm(project_repo_url) == incoming:
        return "main"

    for doc in context_docs:
        if isinstance(doc, dict) and doc.get("repo_url"):
            if _norm(doc["repo_url"]) == incoming:
                return doc.get("name", "")

    return None  # no match


async def trigger_merge_pipeline(
    db, redis, project_id: str, *, pr_number: int, pr_title: str = "",
    branch_name: str = "", commit_hash: str = "", repo_url: str = "",
) -> str | None:
    """Create a pipeline run and spawn the executor. Returns run_id or None."""
    project = await db.fetchrow(
        "SELECT name, repo_url, context_docs, settings_json FROM projects WHERE id = $1",
        project_id,
    )
    if not project:
        return None

    from agents.utils.settings_helpers import parse_settings
    settings = parse_settings(project["settings_json"])

    context_docs = project.get("context_docs") or []
    if isinstance(context_docs, str):
        context_docs = json.loads(context_docs)

    # Match the webhook's repo URL to a repo name
    repo_name = _match_repo_name(repo_url, project.get("repo_url") or "", context_docs)
    if repo_name is None:
        logger.info("[pipeline] No repo match for %s in project %s", repo_url, project_id[:8])
        return None

    # Look up per-repo pipeline config
    all_pipelines = settings.get("merge_pipelines", {})
    mp_config = all_pipelines.get(repo_name, {})
    if not mp_config.get("enabled"):
        return None

    test_cfg = mp_config.get("test_config") or {}
    deploy_cfg = mp_config.get("deploy_config") or {}
    test_mode = test_cfg.get("mode", "poll")

    webhook_token = None
    if test_mode == "webhook":
        webhook_token = secrets.token_urlsafe(32)

    row = await db.fetchrow(
        """
        INSERT INTO pipeline_runs (
            project_id, repo_name, pr_number, pr_title, branch_name, commit_hash,
            repo_url, status, test_mode, test_config, deploy_config,
            webhook_token, started_at
        ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending', $8, $9, $10, $11, NOW())
        RETURNING id
        """,
        project_id, repo_name, pr_number, pr_title, branch_name, commit_hash,
        repo_url, test_mode, test_cfg, deploy_cfg, webhook_token,
    )
    run_id = str(row["id"])

    logger.info(
        "[pipeline] Created run %s for PR #%d in project %s (mode=%s)",
        run_id[:8], pr_number, project_id[:8], test_mode,
    )

    # Spawn executor as background task
    executor = MergePipelineExecutor(db, redis, run_id, project_id)
    asyncio.create_task(executor.execute())

    # Publish initial event
    await _publish(redis, project_id, {
        "type": "pipeline_status",
        "run_id": run_id,
        "status": "pending",
        "pr_number": pr_number,
        "branch_name": branch_name,
    })

    return run_id


class MergePipelineExecutor:
    """Executes a single pipeline run: test phase → deploy phase."""

    def __init__(self, db, redis, run_id: str, project_id: str):
        self.db = db
        self.redis = redis
        self.run_id = run_id
        self.project_id = project_id

    async def execute(self):
        run = await self._load_run()
        if not run:
            return

        try:
            # Test phase
            await self._run_test_phase(run)

            # Re-load after test
            run = await self._load_run()
            if not run or run["status"] != "test_passed":
                return

            # Deploy phase (only if enabled in config)
            deploy_cfg = run.get("deploy_config") or {}
            if deploy_cfg.get("enabled"):
                await self._run_deploy_phase(run)
            else:
                logger.info("[pipeline:%s] Deploy not enabled, skipping", self.run_id[:8])
                await self._update_status("deploy_success", deploy_result={"skipped": True})

        except asyncio.CancelledError:
            await self._update_status("cancelled")
        except Exception as e:
            logger.error("[pipeline:%s] Failed: %s", self.run_id[:8], e, exc_info=True)
            # Status should already be set by phase handlers

    # ── Test Phase ────────────────────────────────────────────────

    async def _run_test_phase(self, run: dict):
        test_config = run.get("test_config") or {}
        mode = test_config.get("mode", "poll")
        await self._update_status("testing")

        if mode == "webhook":
            await self._wait_for_webhook(run, test_config)
        else:
            await self._poll_test_result(run, test_config)

    async def _poll_test_result(self, run: dict, test_config: dict):
        import httpx

        variables = self._build_variables(run)
        poll_url = interpolate(test_config.get("poll_url", ""), variables)
        if not poll_url:
            await self._update_status(
                "test_failed", test_result={"passed": False, "output": "No poll_url configured"},
            )
            return

        interval = test_config.get("poll_interval_seconds", 10)
        timeout = test_config.get("timeout_minutes", 15)
        success_value = test_config.get("poll_success_value", "passed").lower()
        raw_headers = test_config.get("poll_headers") or {}
        headers = {k: interpolate(v, variables) for k, v in raw_headers.items()}
        deadline = time.monotonic() + timeout * 60

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                while time.monotonic() < deadline:
                    # Check if cancelled
                    current = await self._load_run()
                    if not current or current["status"] == "cancelled":
                        return

                    try:
                        resp = await client.get(poll_url, headers=headers)
                        data = resp.json()
                    except Exception as e:
                        logger.debug("[pipeline:%s] Poll error: %s", self.run_id[:8], e)
                        await asyncio.sleep(interval)
                        continue

                    status_val = str(data.get("status", "")).lower()
                    await self._publish_progress("testing", f"Polling... status={status_val}")

                    if status_val == success_value:
                        await self._update_status(
                            "test_passed",
                            test_result={"passed": True, "output": data},
                            test_completed_at=True,
                        )
                        return
                    if status_val in ("failed", "error", "failure"):
                        await self._update_status(
                            "test_failed",
                            test_result={"passed": False, "output": data},
                            test_completed_at=True,
                        )
                        return

                    await asyncio.sleep(interval)

            await self._update_status(
                "test_failed",
                test_result={"passed": False, "output": f"Timed out after {timeout} minutes"},
                test_completed_at=True,
            )
        except Exception as e:
            await self._update_status(
                "test_failed",
                test_result={"passed": False, "output": str(e)[:500]},
                test_completed_at=True,
            )

    async def _wait_for_webhook(self, run: dict, test_config: dict):
        timeout = test_config.get("timeout_minutes", 30) * 60
        redis_key = f"pipeline_test_result:{self.run_id}"
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            current = await self._load_run()
            if not current or current["status"] == "cancelled":
                return

            result = await self.redis.get(redis_key)
            if result:
                data = json.loads(result)
                await self.redis.delete(redis_key)
                passed = data.get("passed", False)
                await self._update_status(
                    "test_passed" if passed else "test_failed",
                    test_result=data,
                    test_completed_at=True,
                )
                return

            await self._publish_progress("testing", "Waiting for test webhook callback...")
            await asyncio.sleep(2)

        await self._update_status(
            "test_failed",
            test_result={"passed": False, "output": "Webhook callback timed out"},
            test_completed_at=True,
        )

    # ── Deploy Phase ──────────────────────────────────────────────

    async def _run_deploy_phase(self, run: dict):
        deploy_cfg = run.get("deploy_config") or {}
        deploy_type = deploy_cfg.get("deploy_type", "http")
        await self._update_status("deploying")

        try:
            if deploy_type == "kubernetes":
                await self._deploy_kubernetes(run, deploy_cfg)
            else:
                await self._deploy_http(run, deploy_cfg)
        except Exception as e:
            logger.error("[pipeline:%s] Deploy failed: %s", self.run_id[:8], e, exc_info=True)
            await self._update_status(
                "deploy_failed",
                deploy_result={"error": str(e)[:500]},
                deploy_completed_at=True,
            )

    async def _deploy_http(self, run: dict, config: dict):
        import httpx

        variables = self._build_variables(run)
        api_url = interpolate(config.get("api_url", ""), variables)
        if not api_url:
            raise ValueError("No deploy api_url configured")

        method = config.get("http_method", "POST").upper()
        raw_headers = config.get("headers") or {}
        headers = {k: interpolate(v, variables) for k, v in raw_headers.items()}
        body_template = config.get("body_template", "")
        body_str = interpolate(body_template, variables)
        success_codes = config.get("success_status_codes") or [200, 201, 202]

        await self._publish_progress("deploying", f"Calling {method} {api_url}")

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.request(method=method, url=api_url, headers=headers, content=body_str)

            if resp.status_code not in success_codes:
                raise ValueError(f"Deploy returned {resp.status_code}: {resp.text[:500]}")

            try:
                resp_data = resp.json()
            except Exception:
                resp_data = {"raw": resp.text[:1000]}

            await self._update_status(
                "deploy_success",
                deploy_result={"status_code": resp.status_code, "response": resp_data},
                deploy_completed_at=True,
            )

    async def _deploy_kubernetes(self, run: dict, config: dict):
        variables = self._build_variables(run, shell_safe=True)
        commands = config.get("kube_commands") or []
        if not commands:
            raise ValueError("No kube_commands configured")

        kube_context = config.get("kube_context")
        results = []

        for i, cmd_template in enumerate(commands):
            cmd = interpolate(cmd_template, variables)
            if kube_context:
                cmd = f"kubectl --context={kube_context} " + cmd.replace("kubectl ", "", 1)

            await self._publish_progress(
                "deploying", f"Running command {i + 1}/{len(commands)}: {cmd[:100]}"
            )

            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)

            results.append({
                "command": cmd[:200],
                "exit_code": proc.returncode,
                "stdout": stdout.decode()[:1000] if stdout else "",
                "stderr": stderr.decode()[:1000] if stderr else "",
            })

            if proc.returncode != 0:
                await self._update_status(
                    "deploy_failed",
                    deploy_result={"commands": results, "failed_at": i},
                    deploy_completed_at=True,
                )
                return

        await self._update_status(
            "deploy_success",
            deploy_result={"commands": results},
            deploy_completed_at=True,
        )

    # ── Helpers ───────────────────────────────────────────────────

    def _build_variables(self, run: dict, *, shell_safe: bool = False) -> dict[str, str]:
        values = {
            "commit_hash": run.get("commit_hash") or "",
            "branch_name": run.get("branch_name") or "",
            "pr_number": str(run.get("pr_number") or ""),
            "pr_title": run.get("pr_title") or "",
            "repo_url": run.get("repo_url") or "",
            "project_name": run.get("_project_name") or "",
            "run_id": self.run_id,
        }
        if shell_safe:
            return {k: shlex.quote(v) for k, v in values.items()}
        return values

    async def _load_run(self) -> dict | None:
        row = await self.db.fetchrow(
            "SELECT * FROM pipeline_runs WHERE id = $1", self.run_id,
        )
        if not row:
            return None
        result = dict(row)
        # Populate project_name lazily
        project = await self.db.fetchrow(
            "SELECT name FROM projects WHERE id = $1", self.project_id,
        )
        if project:
            result["_project_name"] = project["name"]
        return result

    async def _update_status(
        self, status: str, *,
        test_result: dict | None = None,
        deploy_result: dict | None = None,
        test_completed_at: bool = False,
        deploy_completed_at: bool = False,
    ):
        parts = ["status = $2", "updated_at = NOW()"]
        params: list = [self.run_id, status]
        idx = 3

        if test_result is not None:
            parts.append(f"test_result = ${idx}")
            params.append(json.dumps(test_result, default=str))
            idx += 1
        if deploy_result is not None:
            parts.append(f"deploy_result = ${idx}")
            params.append(json.dumps(deploy_result, default=str))
            idx += 1
        if test_completed_at:
            parts.append("test_completed_at = NOW()")
        if deploy_completed_at:
            parts.append("deploy_completed_at = NOW()")

        await self.db.execute(
            f"UPDATE pipeline_runs SET {', '.join(parts)} WHERE id = $1",
            *params,
        )

        await _publish(self.redis, self.project_id, {
            "type": "pipeline_status",
            "run_id": self.run_id,
            "status": status,
        })

        logger.info("[pipeline:%s] Status → %s", self.run_id[:8], status)

    async def _publish_progress(self, phase: str, message: str):
        await _publish(self.redis, self.project_id, {
            "type": "pipeline_progress",
            "run_id": self.run_id,
            "phase": phase,
            "message": message,
        })


async def _publish(redis, project_id: str, data: dict):
    if redis:
        await redis.publish(
            f"pipeline:{project_id}:events",
            json.dumps(data, default=str),
        )


# =====================================================================
# Post-merge actions — lightweight hooks fired after any PR merge
# =====================================================================


async def run_post_merge_actions(
    db, redis, project_id: str, *,
    pr_number: int, pr_title: str = "", branch_name: str = "",
    commit_hash: str = "", repo_url: str = "",
) -> int:
    """Execute post-merge actions for the matched repo. Returns count executed.

    Actions are configured in settings_json.post_merge_actions (per-repo).
    Two types:
      - webhook: HTTP call with template variable interpolation
      - script: shell command with template variable interpolation (shell-safe)
    """
    project = await db.fetchrow(
        "SELECT name, repo_url, context_docs, settings_json FROM projects WHERE id = $1",
        project_id,
    )
    if not project:
        return 0

    from agents.utils.settings_helpers import parse_settings, read_setting
    settings = parse_settings(project["settings_json"])

    context_docs = project.get("context_docs") or []
    if isinstance(context_docs, str):
        context_docs = json.loads(context_docs)

    repo_name = _match_repo_name(repo_url, project.get("repo_url") or "", context_docs)
    if repo_name is None:
        return 0

    all_actions = read_setting(settings, "git.post_merge_actions", "post_merge_actions", {})
    repo_actions = all_actions.get(repo_name, {})
    if not repo_actions.get("enabled"):
        return 0

    actions = repo_actions.get("actions") or []
    if not actions:
        return 0

    variables = {
        "commit_hash": commit_hash,
        "branch_name": branch_name,
        "pr_number": str(pr_number),
        "pr_title": pr_title,
        "repo_url": repo_url,
        "project_name": project["name"] or "",
    }

    executed = 0
    for action in actions:
        action_type = action.get("type", "webhook")
        try:
            if action_type == "webhook":
                await _exec_webhook_action(action, variables)
            elif action_type == "script":
                await _exec_script_action(action, variables)
            else:
                logger.warning("[post-merge] Unknown action type: %s", action_type)
                continue
            executed += 1
        except Exception as e:
            logger.error(
                "[post-merge] Action failed (project=%s repo=%s type=%s): %s",
                project_id[:8], repo_name, action_type, e, exc_info=True,
            )
            # Continue executing remaining actions — don't let one failure block others

    if executed:
        logger.info(
            "[post-merge] Executed %d/%d actions for project %s repo %s (PR #%d)",
            executed, len(actions), project_id[:8], repo_name, pr_number,
        )
    return executed


async def _exec_webhook_action(action: dict, variables: dict[str, str]) -> None:
    """Fire an HTTP webhook with template variable interpolation."""
    import httpx

    url = interpolate(action.get("url", ""), variables)
    if not url:
        raise ValueError("Webhook action has no URL")

    method = action.get("method", "POST").upper()
    raw_headers = action.get("headers") or {}
    headers = {k: interpolate(v, variables) for k, v in raw_headers.items()}

    body_str = None
    if action.get("body_template"):
        body_str = interpolate(action["body_template"], variables)

    timeout = action.get("timeout_seconds", 30)

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method=method, url=url, headers=headers, content=body_str,
        )
        logger.info(
            "[post-merge] Webhook %s %s → %d", method, url, resp.status_code,
        )
        # Don't fail on non-2xx — the user might have a webhook that returns 3xx etc.
        # Log it so they can debug, but don't block other actions.
        if resp.status_code >= 400:
            logger.warning(
                "[post-merge] Webhook returned %d: %s", resp.status_code, resp.text[:300],
            )


async def _exec_script_action(action: dict, variables: dict[str, str]) -> None:
    """Execute a shell script with template variable interpolation.

    All interpolated values are shlex.quote'd to prevent injection.
    """
    command_template = action.get("command", "")
    if not command_template:
        raise ValueError("Script action has no command")

    safe_vars = {k: shlex.quote(v) for k, v in variables.items()}
    command = interpolate(command_template, safe_vars)
    timeout = action.get("timeout_seconds", 120)

    logger.info("[post-merge] Executing script: %s", command[:200])
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

    if proc.returncode != 0:
        err_text = stderr.decode(errors="replace")[:500] if stderr else ""
        logger.warning(
            "[post-merge] Script exited %d: %s", proc.returncode, err_text,
        )
        raise RuntimeError(f"Script exited with code {proc.returncode}: {err_text}")

    logger.info("[post-merge] Script completed (exit 0)")


async def recover_in_flight_runs(db, redis):
    """Re-spawn executors for runs that were in-flight when the server restarted."""
    rows = await db.fetch(
        "SELECT id, project_id FROM pipeline_runs WHERE status IN ('pending', 'testing', 'deploying')"
    )
    for row in rows:
        run_id = str(row["id"])
        project_id = str(row["project_id"])
        logger.info("[pipeline] Recovering in-flight run %s", run_id[:8])
        executor = MergePipelineExecutor(db, redis, run_id, project_id)
        asyncio.create_task(executor.execute())
    if rows:
        logger.info("[pipeline] Recovered %d in-flight pipeline runs", len(rows))
