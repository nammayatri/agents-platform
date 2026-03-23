"""Composable prompt building blocks for LLM agents.

Each function produces one section of context. Agents call only the
helpers they need in build_prompt(), keeping prompts lean and focused.

Replaces the monolithic ContextBuilder.build_agent_prompt() and
ContextBuilder.build_iteration_context() with smaller, reusable pieces.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import asyncpg

from agents.agents.registry import (
    build_tools_prompt_block,
    get_agent_definition,
    get_default_system_prompt,
)
from agents.config.settings import settings
from agents.utils.json_helpers import safe_json

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# System prompt
# ------------------------------------------------------------------


async def get_role_system_prompt(
    role: str,
    db: asyncpg.Pool,
    todo: dict,
    agent_config: dict | None = None,
) -> str:
    """Build the base system prompt for a role.

    Uses custom agent config prompt if available, otherwise the
    hardcoded default from the registry.  Appends structured output
    instructions and tool descriptions.
    """
    if agent_config and agent_config.get("system_prompt"):
        system = agent_config["system_prompt"]
    else:
        system = get_default_system_prompt(role)

    # Role-specific extra context
    if role == "debugger":
        debug_block = await _build_debug_context_block(db, todo)
        if debug_block:
            system += debug_block

    if role == "tester":
        tester_block = await _build_tester_context(db, todo)
        if tester_block:
            system += tester_block

    # Structured output instruction
    from agents.orchestrator.output_validator import build_structured_output_instruction
    system += build_structured_output_instruction(role)

    return system


# ------------------------------------------------------------------
# Workspace context
# ------------------------------------------------------------------


async def get_workspace_context(
    workspace_path: str,
    *,
    cached_repo_map: str | None = None,
    max_depth: int = 4,
) -> dict[str, Any]:
    """Build workspace context dict with file tree, repo map, and deps info.

    Returns::
        {
            "file_tree": str,       # always present
            "repo_map": str | None, # tree-sitter repo map
            "deps_info": str,       # dependency repos block
            "cross_repo_ctx": str,  # .context/ docs pointer
        }
    """
    from agents.orchestrator.workspace import WorkspaceManager

    mgr = WorkspaceManager.__new__(WorkspaceManager)
    file_tree_text = mgr.get_file_tree(workspace_path, max_depth=max_depth)
    workspace_block = (
        f"\n\nYou are working inside the project repository root directory.\n"
        f"Project file structure:\n{file_tree_text}\n"
    )

    # List available dependency repos
    deps_info = ""
    task_deps_dir = os.path.join(workspace_path, "deps")
    if os.path.isdir(task_deps_dir):
        dep_entries = [d for d in sorted(os.listdir(task_deps_dir))
                       if os.path.isdir(os.path.join(task_deps_dir, d))]
        if dep_entries:
            deps_info = (
                "\nDependency repos available (read-only) via ../deps/{name}/path:\n"
            )
            for d in dep_entries:
                deps_info += f"  - ../deps/{d}/\n"

    # Point to .context/ docs
    cross_repo_ctx = ""
    context_dir = os.path.join(workspace_path, ".context")
    if os.path.isdir(context_dir):
        ctx_files = []
        if os.path.isfile(os.path.join(context_dir, "UNDERSTANDING.md")):
            ctx_files.append("- ../.context/UNDERSTANDING.md — main repo architecture, patterns, tech stack")
        if os.path.isfile(os.path.join(context_dir, "LINKING.md")):
            ctx_files.append("- ../.context/LINKING.md — how repos relate, data flow, integration patterns")
        deps_ctx = os.path.join(context_dir, "deps")
        if os.path.isdir(deps_ctx):
            dep_files = sorted(f for f in os.listdir(deps_ctx) if f.endswith(".md"))
            for df in dep_files:
                name = df.removesuffix(".md")
                ctx_files.append(f"- ../.context/deps/{df} — {name} dependency architecture and API")
        if ctx_files:
            cross_repo_ctx = (
                "\n\nProject context docs available (read with read_file when needed):\n"
                + "\n".join(ctx_files)
                + "\n"
            )

    # Repo map
    repo_map_text = cached_repo_map
    if repo_map_text is None:
        repo_map_text = await _build_repo_map(workspace_path)

    # Git diff (for iterative context)
    diff_stat = ""
    try:
        repo_dir = os.path.join(workspace_path, "repo")
        if os.path.isdir(repo_dir):
            proc = await asyncio.create_subprocess_exec(
                "git", "diff", "--stat",
                cwd=repo_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            diff_stat = stdout.decode(errors="replace").strip()
    except Exception:
        pass

    # Tool descriptions
    tools_block = build_tools_prompt_block(role="coder")

    return {
        "file_tree": workspace_block + deps_info + cross_repo_ctx + tools_block,
        "repo_map": repo_map_text,
        "diff_stat": diff_stat,
    }


# ------------------------------------------------------------------
# Previous results
# ------------------------------------------------------------------


async def get_previous_results(db: asyncpg.Pool, todo_id: str) -> str:
    """Summarize completed subtasks for context injection."""
    rows = await db.fetch(
        "SELECT agent_role, title, output_result FROM sub_tasks "
        "WHERE todo_id = $1 AND status = 'completed'",
        todo_id,
    )
    if not rows:
        return ""

    items = []
    for r in rows:
        out = r.get("output_result", {})
        summary = ""
        if isinstance(out, dict):
            summary = out.get("approach", "") or out.get("summary", "")
        items.append(
            f"- [{r.get('agent_role', '?')}] {r.get('title', '?')}: {summary[:300]}"
        )
    return "\n\nCompleted sub-tasks:\n" + "\n".join(items)


# ------------------------------------------------------------------
# Todo summary
# ------------------------------------------------------------------


def get_todo_summary(todo: dict) -> str:
    """Build a concise task metadata block."""
    parts = [f"# Task: {todo['title']}"]
    if todo.get("description"):
        parts.append(f"**Task description:** {todo['description']}")

    intake = safe_json(todo.get("intake_data"))
    if intake and not isinstance(intake, str):
        clean = {k: v for k, v in intake.items() if k != "previous_run" and v}
        if clean:
            parts.append(f"\n**Requirements:** {json.dumps(clean, default=str)}")

    return "\n".join(parts)


# ------------------------------------------------------------------
# Iteration context
# ------------------------------------------------------------------


def get_iteration_context(iteration_log: list[dict], iteration: int) -> str:
    """Format iteration log for RALPH context injection."""
    if not iteration_log:
        return ""

    parts = [f"\n## Previous Iteration Learnings (iteration {iteration})"]
    recent = iteration_log[-5:]
    for entry in recent:
        status = "PASSED" if entry.get("outcome") == "passed" else f"FAILED ({entry.get('outcome', '?')})"
        parts.append(f"- Iteration {entry.get('iteration', '?')}: {status}")
        if entry.get("learnings"):
            parts.append("  " + "; ".join(entry["learnings"]))
        if entry.get("error_output"):
            err = entry["error_output"][:500]
            parts.append(f"  Error: {err}")
    return "\n".join(parts)


# ------------------------------------------------------------------
# Work rules
# ------------------------------------------------------------------


def get_work_rules_prompt(work_rules: dict) -> str:
    """Format work rules into a prompt block."""
    if not work_rules:
        return ""
    parts = ["\n\n## Work Rules (you MUST follow these)\n"]
    for category, items in work_rules.items():
        if items:
            parts.append(f"### {category.title()}")
            for item in items:
                parts.append(f"- {item}")
    return "\n".join(parts)


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


async def _build_repo_map(workspace_path: str) -> str | None:
    """Generate tree-sitter + PageRank repo map."""
    try:
        from agents.indexing.indexer import RepoIndexer
        from agents.indexing.repo_map import render_repo_map
        from agents.utils.token_counter import count_tokens

        repo_dir = os.path.join(workspace_path, "repo")
        if not os.path.isdir(repo_dir):
            return None

        idx_dir = os.path.join(workspace_path, ".agent_index")
        indexer = RepoIndexer()
        graph = indexer.index(repo_dir, cache_dir=idx_dir)
        if graph.symbol_count == 0:
            return None

        return render_repo_map(
            graph,
            token_budget=settings.repo_map_token_budget,
            count_tokens_fn=lambda t: count_tokens(t, "default"),
        )
    except ImportError:
        logger.debug("tree-sitter indexing not available")
    except Exception:
        logger.debug("Repo map generation failed", exc_info=True)
    return None


async def _build_debug_context_block(db: asyncpg.Pool, todo: dict) -> str:
    """Build debug context for debugger agents."""
    project = await db.fetchrow(
        "SELECT settings_json, context_docs FROM projects WHERE id = $1",
        todo["project_id"],
    )
    if not project:
        return ""

    proj_settings = project.get("settings_json") or {}
    if isinstance(proj_settings, str):
        proj_settings = json.loads(proj_settings)
    debug_ctx = proj_settings.get("debug_context") or {}

    parts: list[str] = []

    # Log sources
    log_sources = debug_ctx.get("log_sources") or []
    if log_sources:
        parts.append("\n\n## Log Sources")
        for src in log_sources:
            parts.append(f"### {src.get('service_name', 'Service')}")
            if src.get("log_path"):
                parts.append(f"- **Log path:** `{src['log_path']}`")
            if src.get("log_command"):
                parts.append(f"- **Log command:** `{src['log_command']}`")

    # MCP data hints
    mcp_hints = debug_ctx.get("mcp_hints") or []
    if mcp_hints:
        parts.append("\n\n## MCP Data Sources")
        for hint in mcp_hints:
            name = hint.get("mcp_server_name", "MCP Server")
            parts.append(f"### {name}")
            available = hint.get("available_data") or []
            if available:
                parts.append("**Available data:** " + ", ".join(available))

    # Custom instructions
    custom = debug_ctx.get("custom_instructions") or ""
    if custom:
        parts.append(f"\n\n## Debug Instructions\n{custom}")

    if not parts:
        parts.append(
            "\n\n## Debug Context\n"
            "No debug context configured. Use codebase exploration, "
            "error messages, and available MCP tools to investigate."
        )

    return "\n".join(parts)


async def _build_tester_context(db: asyncpg.Pool, todo: dict) -> str:
    """Build tester context with build/test commands."""
    project = await db.fetchrow(
        "SELECT settings_json FROM projects WHERE id = $1",
        todo["project_id"],
    )
    proj_settings = safe_json(project.get("settings_json")) if project else {}
    build_commands = proj_settings.get("build_commands", [])

    if build_commands:
        cmds = "\n".join(f"  - `{cmd}`" for cmd in build_commands)
        return (
            f"\n\n## Project Build & Test Commands\n"
            f"The project has these configured build/test commands:\n{cmds}\n"
            f"You MUST run these commands to validate the implementation."
        )
    else:
        return (
            "\n\n## Build & Test Discovery\n"
            "No build/test commands configured. Discover them by checking:\n"
            "- `package.json` > scripts.test, scripts.build, scripts.lint\n"
            "- `Makefile` or `Taskfile.yml`\n"
            "- `pyproject.toml` / `setup.py` / `tox.ini`\n"
            "- `Cargo.toml` (cargo test)\n"
            "- CI config files (`.github/workflows/`, `.gitlab-ci.yml`)\n\n"
            "Run the discovered test/build commands to validate."
        )
