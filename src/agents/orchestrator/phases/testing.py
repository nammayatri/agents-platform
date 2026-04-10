"""Testing phase — install deps, build, run tests, create fix subtasks."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from agents.agents.registry import build_tools_prompt_block
from agents.schemas.agent import LLMMessage
from agents.utils.json_helpers import parse_llm_json

if TYPE_CHECKING:
    from agents.orchestrator.coordinator import AgentCoordinator
    from agents.providers.base import AIProvider

logger = logging.getLogger(__name__)

TESTING_SYSTEM_PROMPT = (
    "You are a build and test verification agent. Your job is to verify that the implemented "
    "code changes can be built, dependencies are installed, and all tests pass.\n\n"
    "## Workflow\n"
    "1. Install dependencies (npm install, pip install, cargo build, etc.)\n"
    "2. Run build commands to ensure the project compiles/builds\n"
    "3. Run the full test suite\n"
    "4. Report results with specific pass/fail details\n\n"
    "## Rules\n"
    "- Run ALL provided commands; do not skip any\n"
    "- If a command fails, capture the full error output\n"
    "- Do not attempt to fix code -- only report results\n"
    "- Be thorough: check for missing dependencies, broken imports, type errors\n"
)

_MAX_TEST_RETRIES = 2  # Max times to loop back for test fixes


class TestingPhase:
    """Dedicated testing phase: install deps, build, run tests."""

    def __init__(self, coord: AgentCoordinator) -> None:
        self._coord = coord

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, todo: dict, provider: AIProvider) -> None:
        """Dedicated testing phase: install deps, build, run tests.

        Transitions to review on success, back to in_progress if fixes needed.
        """
        coord = self._coord

        # Transition to testing state
        current = await coord.db.fetchval(
            "SELECT state FROM todo_items WHERE id = $1", coord.todo_id,
        )
        if current == "testing":
            logger.info("[%s] Already in testing state", coord.todo_id)
        elif current == "in_progress":
            await coord._transition_todo("testing", sub_state="build_and_test")
        else:
            logger.warning("[%s] Cannot enter testing from state=%s", coord.todo_id, current)
            return

        await coord._post_system_message(
            "**Entering testing phase.** Installing dependencies, building, and running tests..."
        )

        # 1. Resolve workspace
        # setup_task_workspace returns task_root; main repo is at repos/main/
        from agents.orchestrator.workspace import MAIN_REPO
        workspace_path = None
        try:
            project = await coord.db.fetchrow(
                "SELECT repo_url FROM projects WHERE id = $1", todo["project_id"]
            )
            if project and project.get("repo_url"):
                task_root = await coord.workspace_mgr.setup_task_workspace(coord.todo_id)
                workspace_path = os.path.join(task_root, MAIN_REPO)
        except Exception:
            logger.exception("[%s] Could not set up workspace for testing — skipping to review", coord.todo_id)

        if not workspace_path:
            logger.info("[%s] No workspace for testing, skipping to review", coord.todo_id)
            todo = await coord._load_todo()
            await coord._review.run(todo, provider)
            return

        repo_dir = workspace_path

        # 2. Resolve build/test commands from project settings and work rules
        from agents.utils.work_rules import resolve_work_rules
        project = await coord.db.fetchrow("SELECT settings_json FROM projects WHERE id = $1", todo["project_id"])
        work_rules = resolve_work_rules(todo, dict(project) if project else None)
        project_settings = await self._get_project_settings(todo)

        from agents.utils.settings_helpers import get_build_command_strings
        build_commands = get_build_command_strings(project_settings)
        quality_commands = work_rules.get("quality", [])
        testing_rules = work_rules.get("testing", [])

        has_explicit_commands = bool(build_commands or quality_commands or testing_rules)

        # 3. Run tests
        if has_explicit_commands:
            test_results = await self._run_testing_commands(
                repo_dir, build_commands, quality_commands, testing_rules,
            )
        else:
            test_results = await self._run_testing_with_discovery(
                todo, provider, workspace_path,
            )

        # 4. Evaluate results
        if test_results["passed"]:
            summary = test_results.get("summary", "All checks passed.")
            await coord._post_system_message(
                f"**Testing passed.** {summary}\n\nProceeding to review."
            )
            await coord.db.execute(
                "UPDATE todo_items SET sub_state = 'tests_passed', updated_at = NOW() WHERE id = $1",
                coord.todo_id,
            )
            todo = await coord._load_todo()
            await coord._review.run(todo, provider)
        else:
            # Count previous test-fix attempts
            test_fix_count = await coord.db.fetchval(
                "SELECT COUNT(*) FROM sub_tasks "
                "WHERE todo_id = $1 AND title LIKE 'Fix test failures from testing phase%'",
                coord.todo_id,
            ) or 0
            if test_fix_count < _MAX_TEST_RETRIES:
                await self._create_test_fix_subtasks(
                    todo, provider, test_results, workspace_path,
                )
                await coord._transition_todo(
                    "in_progress", sub_state="fixing_test_failures",
                )
                # Return to scheduler — it will pick up in_progress and run _execute_jobs
                return
            else:
                error_output = test_results.get("error_output", "Unknown failures")
                await coord._post_system_message(
                    f"**Testing failed after {test_fix_count + 1} attempts.**\n\n"
                    f"Failures:\n```\n{error_output[:1000]}\n```\n\n"
                    "Proceeding to review with known test failures."
                )
                todo = await coord._load_todo()
                await coord._review.run(todo, provider)

    # ------------------------------------------------------------------
    # Command execution helpers
    # ------------------------------------------------------------------

    async def _run_command_phase(
        self, repo_dir: str, commands: list[str], phase: str,
        timeout: int, steps: list[dict],
    ) -> bool:
        """Run a list of commands for a single testing phase."""
        coord = self._coord
        all_passed = True
        for cmd in commands:
            try:
                exit_code, output = await coord.workspace_mgr.run_command(cmd, repo_dir, timeout=timeout)
                passed = exit_code == 0
                steps.append({"command": cmd, "passed": passed, "output": output[:2000], "phase": phase})
                if not passed:
                    all_passed = False
                await self._publish_testing_progress(cmd, passed)
            except Exception as e:
                steps.append({"command": cmd, "passed": False, "output": str(e)[:500], "phase": phase})
                all_passed = False
                await self._publish_testing_progress(cmd, False)
        return all_passed

    async def _run_testing_commands(
        self, repo_dir: str, build_commands: list[str],
        quality_commands: list[str], testing_rules: list[str],
    ) -> dict:
        """Run explicit build/test commands and return results."""
        steps: list[dict] = []
        all_passed = True

        dep_install_cmds = self._detect_dependency_install_commands(repo_dir)

        for commands, phase, timeout in [
            (dep_install_cmds, "install", 180),
            (build_commands, "build", 120),
            (quality_commands, "quality", 120),
            (testing_rules, "test", 180),
        ]:
            if not await self._run_command_phase(repo_dir, commands, phase, timeout, steps):
                all_passed = False

        failed_steps = [s for s in steps if not s["passed"]]
        error_output = "\n".join(
            f"[FAIL] {s['command']}:\n{s['output']}" for s in failed_steps
        ) if failed_steps else None

        summary = f"{len(steps)} commands run, {len(failed_steps)} failed" if steps else "No commands to run"
        return {
            "passed": all_passed,
            "summary": summary,
            "error_output": error_output,
            "steps": steps,
        }

    @staticmethod
    def _detect_dependency_install_commands(repo_dir: str) -> list[str]:
        """Auto-detect dependency installation commands from the repo."""
        commands: list[str] = []

        # Node.js
        package_json = os.path.join(repo_dir, "package.json")
        if os.path.isfile(package_json):
            pnpm_lock = os.path.join(repo_dir, "pnpm-lock.yaml")
            yarn_lock = os.path.join(repo_dir, "yarn.lock")
            lock_file = os.path.join(repo_dir, "package-lock.json")
            if os.path.isfile(pnpm_lock):
                commands.append("pnpm install --frozen-lockfile")
            elif os.path.isfile(yarn_lock):
                commands.append("yarn install --frozen-lockfile")
            elif os.path.isfile(lock_file):
                commands.append("npm ci")
            else:
                commands.append("npm install")

        # Python
        requirements = os.path.join(repo_dir, "requirements.txt")
        pyproject = os.path.join(repo_dir, "pyproject.toml")
        if os.path.isfile(requirements):
            commands.append("pip install -r requirements.txt")
        elif os.path.isfile(pyproject):
            commands.append("pip install -e .")

        # Rust
        cargo_toml = os.path.join(repo_dir, "Cargo.toml")
        if os.path.isfile(cargo_toml):
            commands.append("cargo build")

        # Go
        go_mod = os.path.join(repo_dir, "go.mod")
        if os.path.isfile(go_mod):
            commands.append("go mod download")

        return commands

    async def _run_testing_with_discovery(
        self, todo: dict, provider: AIProvider, workspace_path: str,
    ) -> dict:
        """Use an LLM agent to discover and run build/test commands."""
        coord = self._coord
        repo_dir = workspace_path

        # Install detected dependencies first
        dep_cmds = self._detect_dependency_install_commands(repo_dir)
        for cmd in dep_cmds:
            try:
                await coord.workspace_mgr.run_command(cmd, repo_dir, timeout=180)
                await self._publish_testing_progress(cmd, True)
            except Exception:
                logger.warning("[%s] Dep install failed: %s", coord.todo_id, cmd, exc_info=True)
                await self._publish_testing_progress(cmd, False)

        # Build system prompt for LLM-driven test discovery
        tester_context = await coord._ctx.build_tester_context(todo)
        system_prompt = TESTING_SYSTEM_PROMPT + tester_context
        system_prompt += build_tools_prompt_block("tester")

        # Add file tree for orientation
        try:
            file_tree = coord.workspace_mgr.get_file_tree(workspace_path, max_depth=3)
            system_prompt += f"\n\nProject file structure:\n{file_tree}\n"
        except Exception:
            pass

        messages = [
            LLMMessage(role="system", content=system_prompt),
            LLMMessage(role="user", content=(
                "Discover and run all build and test commands for this project. "
                "Check package.json, pyproject.toml, Makefile, CI configs, etc. "
                "Install any missing dependencies, then run the commands and report results.\n\n"
                'You MUST output JSON at the end: {"passed": true/false, "summary": "...", '
                '"commands_run": ["cmd1", "cmd2"], "failures": ["failure detail"]}'
            )),
        ]

        tools = coord._get_builtin_tools(workspace_path, "tester")

        # Add submit_result tool for structured test output
        from agents.orchestrator.structured_output import build_submit_tool, extract_submit_result
        from agents.providers.base import run_tool_loop

        test_schema = {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean", "description": "True if all tests passed"},
                "summary": {"type": "string", "description": "Brief summary of results"},
                "commands_run": {"type": "array", "items": {"type": "string"}},
                "failures": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["passed", "summary"],
        }
        test_submit_tool = build_submit_tool(test_schema, "test_results",
            "Submit test results. Call this when you have finished running all tests.")
        tools_with_submit = list(tools) + [test_submit_tool]

        async def _test_tool_exec(name: str, args: dict) -> str:
            if name == "submit_result":
                return json.dumps({"status": "received"})
            return await coord.mcp_executor.execute_tool(name, args, tools)

        max_parse_retries = 3
        last_content = ""
        for parse_attempt in range(max_parse_retries):
            try:
                _test_token_cb = coord._build_token_streamer()
                content, response = await run_tool_loop(
                    provider, messages,
                    tools=tools_with_submit,
                    tool_executor=_test_tool_exec,
                    max_rounds=500,
                    on_cancel_check=coord._is_cancelled,
                    on_token=_test_token_cb,
                    temperature=0.1,
                )
                if hasattr(_test_token_cb, "flush"):
                    await _test_token_cb.flush()
                await coord._track_tokens(response)
                last_content = content

                # Try submit_result first, then text fallback
                result = extract_submit_result(
                    response.tool_calls, content, messages=messages,
                )
                if result is None:
                    result = parse_llm_json(content)
                if result is not None and isinstance(result.get("passed"), bool):
                    return {
                        "passed": result["passed"],
                        "summary": result.get("summary", ""),
                        "error_output": "\n".join(result.get("failures", [])) if result.get("failures") else None,
                    }

                # Parse failed — retry with correction prompt
                logger.warning(
                    "[%s] Test discovery parse attempt %d/%d failed, retrying with correction",
                    coord.todo_id, parse_attempt + 1, max_parse_retries,
                )
                if parse_attempt < max_parse_retries - 1:
                    messages = [
                        LLMMessage(role="system", content=system_prompt),
                        LLMMessage(role="user", content=(
                            "Your previous response could not be parsed as valid JSON. "
                            "You MUST respond with ONLY a JSON object in this exact format, "
                            "no other text:\n"
                            '{"passed": true, "summary": "what happened", '
                            '"commands_run": ["cmd1"], "failures": ["failure1"]}\n\n'
                            "Set passed=true if all tests passed, false if any failed."
                        )),
                    ]
            except Exception as e:
                logger.error("[%s] LLM test discovery attempt %d failed: %s",
                             coord.todo_id, parse_attempt + 1, e, exc_info=True)
                last_content = str(e)
                if parse_attempt < max_parse_retries - 1:
                    continue
                break

        # All retries exhausted — treat as failure
        logger.error("[%s] Test discovery failed after %d parse attempts", coord.todo_id, max_parse_retries)
        return {
            "passed": False,
            "summary": "Test discovery could not produce parseable results after retries",
            "error_output": f"LLM output could not be parsed:\n{last_content[:1000]}",
        }

    # ------------------------------------------------------------------
    # Fix subtask creation
    # ------------------------------------------------------------------

    async def _create_test_fix_subtasks(
        self, todo: dict, provider: AIProvider, test_results: dict,
        workspace_path: str | None,
    ) -> None:
        """When testing fails, create coder subtask(s) to fix the issues."""
        coord = self._coord
        steps = test_results.get("steps", [])
        failed_steps = [s for s in steps if not s.get("passed")]

        if not failed_steps:
            # Fallback: no structured steps, use raw error_output
            error_output = test_results.get("error_output", "Unknown test failures")
            await coord._lifecycle.create_guardrail_subtask(
                title="Fix test failures from testing phase",
                description=(
                    "The testing phase found failures that need to be fixed:\n\n"
                    f"```\n{error_output[:3000]}\n```\n\n"
                    "Investigate these failures and fix the underlying code issues. "
                    "Do NOT disable tests or skip checks -- fix the actual problems."
                ),
                role="coder",
                depends_on=[],
            )
            await coord._post_system_message(
                f"**Testing failed.** Creating fix subtask and retrying.\n\n"
                f"Failures:\n```\n{error_output[:500]}\n```"
            )
            return

        # Group failed steps by phase
        by_phase: dict[str, list[dict]] = {}
        for step in failed_steps:
            phase = step.get("phase", "test")
            by_phase.setdefault(phase, []).append(step)

        phase_labels = {
            "install": "dependency installation",
            "build": "build",
            "quality": "quality/lint checks",
            "test": "test suite",
        }

        created_ids = []
        for phase, phase_steps in by_phase.items():
            label = phase_labels.get(phase, phase)
            errors_block = "\n\n".join(
                f"**Command:** `{s['command']}`\n```\n{s['output'][:1500]}\n```"
                for s in phase_steps
            )
            fix_desc = (
                f"The {label} failed during the testing phase. Fix the issues below.\n\n"
                f"{errors_block}\n\n"
                "Fix the actual problems in the source code. "
                "Do NOT disable tests, skip checks, or remove failing commands."
            )
            st_id = await coord._lifecycle.create_guardrail_subtask(
                title=f"Fix {label} failures",
                description=fix_desc,
                role="coder",
                depends_on=[],
            )
            created_ids.append(st_id)

        summary_lines = []
        for phase, phase_steps in by_phase.items():
            cmds = ", ".join(f"`{s['command']}`" for s in phase_steps)
            summary_lines.append(f"- **{phase_labels.get(phase, phase)}**: {cmds}")

        await coord._post_system_message(
            f"**Testing failed.** Creating {len(created_ids)} fix subtask(s) and retrying.\n\n"
            + "\n".join(summary_lines)
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_project_settings(self, todo: dict) -> dict:
        """Fetch and parse project settings_json."""
        from agents.utils.settings_helpers import parse_settings
        coord = self._coord
        project = await coord.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", todo["project_id"]
        )
        return parse_settings((project or {}).get("settings_json"))

    async def _publish_testing_progress(self, command: str, passed: bool) -> None:
        """Publish a testing progress event to the WebSocket channel."""
        coord = self._coord
        try:
            status = "passed" if passed else "failed"
            await coord.redis.publish(
                f"task:{coord.todo_id}:events",
                json.dumps({
                    "type": "testing_step",
                    "command": command,
                    "status": status,
                }),
            )
        except Exception:
            pass
