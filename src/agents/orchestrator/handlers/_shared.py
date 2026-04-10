"""Shared utility functions extracted from SubtaskLifecycle.

These are used by multiple handler modules and converted from
``self._coord.xyz`` to ``ctx.xyz`` (HandlerContext pattern).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import TYPE_CHECKING

from agents.utils.json_helpers import parse_llm_json

from agents.orchestrator.handlers._base import HandlerContext

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

MAX_REVIEW_ROUNDS = 5


# ──────────────────────────────────────────────────────────────────────
# Git provider resolution for subtasks (handles target_repo for deps)
# ──────────────────────────────────────────────────────────────────────

async def resolve_git_for_subtask(
    ctx: HandlerContext, sub_task: dict, project: dict,
) -> tuple:
    """Resolve git provider, owner, and repo for a subtask.

    If the subtask has a target_repo (dependency), resolves from that.
    Otherwise falls back to the main project repo.

    Returns (git_provider_instance, owner, repo).
    """
    from agents.orchestrator.git_providers.factory import (
        create_git_provider,
        parse_repo_url,
    )
    from agents.infra.crypto import decrypt

    target_repo = sub_task.get("target_repo")
    if isinstance(target_repo, str):
        target_repo = json.loads(target_repo)

    if target_repo and target_repo.get("repo_url"):
        repo_url = target_repo["repo_url"]
        git_provider_id = target_repo.get("git_provider_id")
    else:
        repo_url = project.get("repo_url")
        git_provider_id = str(project["git_provider_id"]) if project.get("git_provider_id") else None

    if not repo_url:
        raise ValueError("No repo_url available for subtask")

    token = None
    provider_type = None
    api_base_url = None
    if git_provider_id:
        gp_row = await ctx.db.fetchrow(
            "SELECT provider_type, api_base_url, token_enc "
            "FROM git_provider_configs WHERE id = $1",
            git_provider_id,
        )
        if gp_row:
            token = decrypt(gp_row["token_enc"]) if gp_row.get("token_enc") else None
            provider_type = gp_row["provider_type"]
            api_base_url = gp_row.get("api_base_url")

    git = create_git_provider(
        provider_type=provider_type,
        api_base_url=api_base_url,
        token=token,
        repo_url=repo_url,
    )
    owner, repo_name = parse_repo_url(repo_url)
    return git, owner, repo_name


# ──────────────────────────────────────────────────────────────────────
# Dependency propagation
# ──────────────────────────────────────────────────────────────────────

async def propagate_dependencies(
    ctx: HandlerContext, parent_subtask_id: str, new_subtask_ids: list[str],
) -> None:
    """When a subtask creates continuation subtasks, propagate to dependents.

    Any subtask that depends on ``parent_subtask_id`` should also depend on
    the newly created subtask IDs so it waits for the full chain to finish.
    """
    if not new_subtask_ids:
        return

    # Find all subtasks that have parent_subtask_id in their depends_on
    dependents = await ctx.db.fetch(
        "SELECT id, depends_on FROM sub_tasks "
        "WHERE todo_id = $1 AND $2 = ANY(depends_on) AND status = 'pending'",
        ctx.todo_id, parent_subtask_id,
    )
    if not dependents:
        return

    for dep in dependents:
        dep_id = str(dep["id"])
        # Skip if the dependent IS one of the new subtasks (avoid circular deps)
        if dep_id in new_subtask_ids:
            continue
        existing = [str(d) for d in (dep["depends_on"] or [])]
        to_add = [sid for sid in new_subtask_ids if sid not in existing and sid != dep_id]
        if not to_add:
            continue
        updated_deps = existing + to_add
        await ctx.db.execute(
            "UPDATE sub_tasks SET depends_on = $2 WHERE id = $1",
            dep["id"], updated_deps,
        )
        logger.info(
            "[%s] Propagated deps: subtask %s now also depends on %s (via parent %s)",
            ctx.todo_id, dep_id[:8], [s[:8] for s in to_add], parent_subtask_id[:8],
        )


# ──────────────────────────────────────────────────────────────────────
# Merge approval check
# ──────────────────────────────────────────────────────────────────────

async def requires_merge_approval(ctx: HandlerContext) -> bool:
    """Check project setting: should we skip auto-merge?"""
    from agents.utils.settings_helpers import parse_settings, read_setting
    todo = await ctx.load_todo()
    proj = await ctx.db.fetchrow(
        "SELECT settings_json FROM projects WHERE id = $1",
        todo["project_id"],
    )
    settings_val = parse_settings((proj or {}).get("settings_json"))
    return bool(read_setting(settings_val, "git.require_merge_approval", "require_merge_approval", False))


# ──────────────────────────────────────────────────────────────────────
# Guardrails
# ──────────────────────────────────────────────────────────────────────

async def ensure_coding_guardrails(ctx: HandlerContext, workspace_path: str | None) -> bool:
    """Create tester/reviewer subtasks per repo for completed coder work.

    Groups completed coders by target_repo. For each repo that doesn't
    already have a tester+reviewer, creates them. Each tester/reviewer
    only validates changes for its specific repo.

    Returns True if new guardrail subtasks were created.
    """
    coder_subtasks = await ctx.db.fetch(
        "SELECT id, title, review_loop, review_chain_id, target_repo, workspace_path "
        "FROM sub_tasks "
        "WHERE todo_id = $1 AND agent_role = 'coder' AND status = 'completed'",
        ctx.todo_id,
    )
    if not coder_subtasks:
        return False

    unreviewed_coders = [
        st for st in coder_subtasks
        if not st["review_loop"] and not st["review_chain_id"]
    ]
    if not unreviewed_coders:
        return False

    # Group coders by repo name
    import json as _json
    coders_by_repo: dict[str, list[dict]] = {}
    for st in unreviewed_coders:
        target = st.get("target_repo")
        if isinstance(target, str):
            try:
                target = _json.loads(target)
            except (ValueError, TypeError):
                target = None
        repo_name = "main"
        if isinstance(target, dict) and target.get("name"):
            repo_name = target["name"]
        elif isinstance(target, str) and target:
            repo_name = target
        coders_by_repo.setdefault(repo_name, []).append(dict(st))

    # Check existing tester/reviewer subtasks per repo
    existing_guardrails = await ctx.db.fetch(
        "SELECT agent_role, target_repo FROM sub_tasks "
        "WHERE todo_id = $1 AND agent_role IN ('tester', 'reviewer')",
        ctx.todo_id,
    )
    # Build set of (repo_name, role) that already exist
    existing_pairs: set[tuple[str, str]] = set()
    for row in existing_guardrails:
        tr = row.get("target_repo")
        if isinstance(tr, str):
            try:
                tr = _json.loads(tr)
            except (ValueError, TypeError):
                pass
        rn = "main"
        if isinstance(tr, dict) and tr.get("name"):
            rn = tr["name"]
        elif isinstance(tr, str) and tr:
            rn = tr
        existing_pairs.add((rn, row["agent_role"]))

    created: list[tuple[str, str]] = []

    for repo_name, coders in coders_by_repo.items():
        coder_ids = [str(st["id"]) for st in coders]
        coder_titles = [st["title"] for st in coders]
        chain_id = coder_ids[0]

        # Get target_repo and workspace_path from first coder
        target_repo_json = coders[0].get("target_repo")
        coder_ws = coders[0].get("workspace_path")

        repo_label = repo_name if repo_name != "main" else "main repo"

        # Tester
        if (repo_name, "tester") not in existing_pairs:
            tester_desc = (
                f"Validate code changes in {repo_label}:\n"
                + "\n".join(f"- {t}" for t in coder_titles)
                + "\n\nRun build, typecheck, lint, and test commands for this repo."
            )
            title = f"Test changes — {repo_name}" if repo_name != "main" else "Test implemented changes"
            tester_id = await _create_guardrail_subtask_with_repo(
                ctx, title=title, description=tester_desc, role="tester",
                depends_on=coder_ids, chain_id=chain_id,
                target_repo=target_repo_json, workspace_path=coder_ws,
            )
            created.append(("tester", tester_id))

        # Reviewer
        if (repo_name, "reviewer") not in existing_pairs:
            deps = list(coder_ids)
            # Reviewer depends on tester if one was just created for this repo
            if (repo_name, "tester") not in existing_pairs and created:
                deps.append(created[-1][1])
            reviewer_desc = (
                f"Review code changes in {repo_label} for quality, security, correctness:\n"
                + "\n".join(f"- {t}" for t in coder_titles)
            )
            title = f"Review changes — {repo_name}" if repo_name != "main" else "Review code changes"
            reviewer_id = await _create_guardrail_subtask_with_repo(
                ctx, title=title, description=reviewer_desc, role="reviewer",
                depends_on=deps, chain_id=chain_id,
                target_repo=target_repo_json, workspace_path=coder_ws,
            )
            created.append(("reviewer", reviewer_id))

    if created:
        all_new_ids = [sid for _, sid in created]
        all_coder_ids = [str(st["id"]) for st in unreviewed_coders]
        for coder_id in all_coder_ids:
            await propagate_dependencies(ctx, coder_id, all_new_ids)

        repos = ", ".join(coders_by_repo.keys())
        roles = ", ".join(sorted(set(r for r, _ in created)))
        logger.info(
            "[%s] Guardrails: created %d subtasks (%s) for repos: %s",
            ctx.todo_id, len(created), roles, repos,
        )
        await ctx.post_system_message(
            f"**Guardrail:** Auto-created {roles} for repos: {repos}"
        )
        return True

    return False


async def _create_guardrail_subtask_with_repo(
    ctx: HandlerContext,
    title: str,
    description: str,
    role: str,
    depends_on: list[str],
    chain_id: str,
    target_repo=None,
    workspace_path: str | None = None,
) -> str:
    """Create a guardrail subtask with repo targeting and return its ID."""
    max_order = 0
    if depends_on:
        rows = await ctx.db.fetch(
            "SELECT execution_order FROM sub_tasks WHERE id = ANY($1)",
            depends_on,
        )
        max_order = max((r["execution_order"] or 0) for r in rows) if rows else 0

    row = await ctx.db.fetchrow(
        """
        INSERT INTO sub_tasks (
            todo_id, title, description, agent_role,
            execution_order, depends_on, review_loop, review_chain_id,
            target_repo, workspace_path
        )
        VALUES ($1, $2, $3, $4, $5, $6, TRUE, $7, $8, $9)
        RETURNING id
        """,
        ctx.todo_id, title, description, role,
        max_order + 1, depends_on, chain_id,
        target_repo, workspace_path,
    )
    return str(row["id"])


async def create_guardrail_subtask(
    ctx: HandlerContext,
    title: str,
    description: str,
    role: str,
    depends_on: list[str],
    *,
    review_loop: bool = False,
    review_chain_id: str | None = None,
) -> str:
    """Create a guardrail subtask and return its ID as string."""
    max_order = 0
    if depends_on:
        rows = await ctx.db.fetch(
            "SELECT execution_order FROM sub_tasks WHERE id = ANY($1)",
            depends_on,
        )
        max_order = max((r["execution_order"] or 0) for r in rows) if rows else 0

    row = await ctx.db.fetchrow(
        """
        INSERT INTO sub_tasks (
            todo_id, title, description, agent_role,
            execution_order, depends_on, review_loop, review_chain_id
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        RETURNING id
        """,
        ctx.todo_id,
        title,
        description,
        role,
        max_order + 1,
        depends_on,
        review_loop,
        review_chain_id,
    )
    st_id = str(row["id"])
    logger.info(
        "[%s] Created guardrail subtask %s: role=%s title=%s depends_on=%s review_loop=%s chain=%s",
        ctx.todo_id, st_id, role, title, depends_on, review_loop, review_chain_id,
    )
    return st_id


# ──────────────────────────────────────────────────────────────────────
# Review verdict extraction (static helper)
# ──────────────────────────────────────────────────────────────────────

def extract_review_verdict(content: str) -> str:
    """Parse reviewer verdict from output content."""
    data = parse_llm_json(content)
    if data is not None:
        verdict = data.get("verdict", "").lower()
        if verdict in ("approved", "needs_changes"):
            return verdict

    lower = content.lower()
    if "approved" in lower and "needs_changes" not in lower:
        return "approved"
    if "needs_changes" in lower or "needs changes" in lower or "request changes" in lower:
        return "needs_changes"

    return "needs_changes"


# ──────────────────────────────────────────────────────────────────────
# Fix description builders (static helpers)
# ──────────────────────────────────────────────────────────────────────

def build_fix_description_for_file(
    file_key: str, issues: list[dict], reviewer_st: dict,
) -> str:
    """Build a focused fix description for issues in a single file."""
    if file_key == "_general":
        parts = ["Fix the following general issues:\n"]
    else:
        parts = [f"Fix the following issues in `{file_key}`:\n"]

    for i, issue in enumerate(issues, 1):
        severity = issue.get("severity", "major").upper()
        line = issue.get("line")
        issue_desc = issue.get("description", "")
        suggestion = issue.get("suggestion", "")

        line_ref = f" (line {line})" if line else ""
        parts.append(f"### Issue {i} [{severity}]{line_ref}")
        if issue_desc:
            parts.append(f"**Problem:** {issue_desc}")
        if suggestion:
            parts.append(f"**Fix:** {suggestion}")
        parts.append("")

    reviewer_output = reviewer_st.get("output_result") or {}
    if isinstance(reviewer_output, dict):
        summary = reviewer_output.get("summary", "")
        if summary:
            parts.append(f"\n## Reviewer Summary\n{summary}")

    return "\n".join(parts)


def build_pre_commit_fix_description(error_output: str) -> str:
    """Build a detailed coder subtask description for fixing pre-commit hook errors."""
    return (
        "The git pre-commit hooks failed when trying to commit your changes. "
        "You MUST fix every error and then successfully commit.\n\n"
        "## Workflow (repeat until commit succeeds)\n\n"
        "1. Read the pre-commit errors below.\n"
        "2. Read each failing file to understand context.\n"
        "3. Fix the errors.\n"
        "4. Stage and commit: `git add -A && git commit -m \"fix: resolve pre-commit hook errors\"`\n"
        "5. If the commit **fails again**, read the NEW error output, fix the new errors, "
        "and run the commit command again. **Keep looping until the commit succeeds.**\n"
        "6. Only call `task_complete` after a successful commit.\n\n"
        "## Critical Rules\n\n"
        "1. **Fix every error** — do NOT skip or ignore any.\n"
        "2. **Do NOT suppress checks** — no `eslint-disable`, `@ts-ignore`, `type: ignore`, "
        "`noqa`, `# noinspection`, `prettier-ignore`, or any other skip/suppress comment.\n"
        "3. **Do NOT revert the functional changes** — only fix the lint/type/style issues "
        "while preserving the intended behavior.\n"
        "4. **Do NOT remove or simplify existing code** just to avoid lint errors. "
        "Rewrite the code to satisfy the rule properly.\n"
        "5. **NEVER use `--no-verify`** — always let pre-commit hooks run.\n"
        "6. **You MUST commit successfully** — do not mark the task complete without a "
        "successful `git commit` (exit code 0).\n\n"
        "## Common Fix Patterns\n\n"
        "- **`functional/immutable-data`** (Modifying object/array/map not allowed): "
        "Use spread syntax (`{...obj, key: value}`), `.map()`, `.filter()`, "
        "`new Map([...oldMap, [key, value]])`, or `Object.assign({}, obj, updates)` "
        "instead of direct mutation.\n"
        "- **`no-as-in-modified-files`** (No type assertions with `as`): "
        "Use `satisfies`, generic type parameters, proper type annotations on variables, "
        "or type guard functions instead of `as` casts.\n"
        "- **`functional/no-let`** (No `let`): "
        "Use `const` with ternary expressions, `Array.reduce()`, or immediately-invoked "
        "functions to avoid mutable bindings.\n"
        "- **`no-console`** (No console.log): "
        "Remove `console.log` calls or replace with allowed methods "
        "(`console.warn`, `console.error`, `console.info`).\n"
        "- **`no-non-null-assertion`** (No `!` assertions): "
        "Use optional chaining (`?.`), nullish coalescing (`??`), "
        "or explicit null checks with early returns.\n"
        "- **`immutable-data` on arrays** (No `.push()`, `.splice()`, etc.): "
        "Use `[...arr, newItem]`, `.concat()`, or `.filter()` to produce new arrays.\n\n"
        "## Pre-Commit Errors\n\n"
        f"```\n{error_output[:6000]}\n```\n\n"
        "**Remember:** Fix → `git add -A && git commit` → if it fails, fix again → repeat. "
        "Do NOT call task_complete until the commit exits with code 0."
    )


# ──────────────────────────────────────────────────────────────────────
# Workspace finalization (commit → push → PR)
# ──────────────────────────────────────────────────────────────────────

async def finalize_subtask_workspace(
    ctx: HandlerContext, sub_task: dict, workspace_path: str | None,
) -> dict | None:
    """Deterministic commit → push → PR for a sub-task's workspace changes."""
    if not workspace_path:
        logger.warning("[%s] Cannot finalize: no workspace_path", ctx.todo_id)
        await ctx.post_system_message("**PR creation skipped:** no workspace path available.")
        return None

    try:
        todo = await ctx.load_todo()
        short_id = str(ctx.todo_id)[:8]

        target_repo = sub_task.get("target_repo")
        if isinstance(target_repo, str):
            target_repo = json.loads(target_repo)

        if target_repo and target_repo.get("repo_url"):
            dep_name = (target_repo.get("name") or "dep").replace("/", "_").replace(" ", "_")
            branch_name = f"task/{short_id}-{dep_name}"
            base_branch = target_repo.get("default_branch") or "main"
        else:
            project = await ctx.db.fetchrow(
                "SELECT repo_url, default_branch FROM projects WHERE id = $1",
                todo["project_id"],
            )
            if not project or not project.get("repo_url"):
                logger.warning("[%s] Cannot finalize: no repo_url on project", ctx.todo_id)
                await ctx.post_system_message("**PR creation skipped:** no repository URL configured on the project.")
                return None
            branch_name = f"task/{short_id}"
            base_branch = project.get("default_branch") or "main"

        logger.info("[%s] Finalizing workspace: branch=%s base=%s dep=%s path=%s",
                    ctx.todo_id, branch_name, base_branch,
                    bool(target_repo), workspace_path)

        # Step 1: commit and push
        commit_result = await ctx.workspace_mgr.commit_and_push(
            workspace_path,
            message=f"[agents] {sub_task['title']}",
            branch=branch_name,
        )

        # If pre-commit hooks failed, signal the caller to create a fix subtask
        if commit_result.get("pre_commit_failed"):
            return {
                "_pre_commit_failed": True,
                "pre_commit_output": commit_result.get("pre_commit_output", ""),
                "workspace_path": workspace_path,
            }

        if not commit_result["success"]:
            error = commit_result.get("error", "unknown error")
            logger.error("[%s] commit_and_push failed for branch %s: %s", ctx.todo_id, branch_name, error)
            await ctx.post_system_message(f"**PR creation failed:** could not push to branch `{branch_name}` — {error}")
            return None

        logger.info("[%s] Pushed to branch %s", ctx.todo_id, branch_name)

        # Step 2: check if PR already exists for this branch
        existing_pr = await ctx.db.fetchrow(
            "SELECT id, pr_number, pr_url FROM deliverables "
            "WHERE todo_id = $1 AND type = 'pull_request' AND branch_name = $2",
            ctx.todo_id,
            branch_name,
        )
        if existing_pr:
            logger.info("[%s] PR already exists: #%s", ctx.todo_id, existing_pr["pr_number"])
            return {"number": existing_pr["pr_number"], "url": existing_pr.get("pr_url")}

        # Step 3: create PR
        if target_repo and target_repo.get("repo_url"):
            pr_info = await ctx.workspace_mgr.create_pr_for_repo(
                repo_url=target_repo["repo_url"],
                git_provider_id=target_repo.get("git_provider_id"),
                head_branch=branch_name,
                base_branch=base_branch,
                title=todo["title"],
                body=f"## {sub_task['title']}\n\n*Created by AI Agent*",
            )
        else:
            pr_info = await ctx.workspace_mgr.create_pr(
                str(todo["project_id"]),
                head_branch=branch_name,
                base_branch=base_branch,
                title=todo["title"],
                body=f"## {sub_task['title']}\n\n*Created by AI Agent*",
            )

        # Step 4: record deliverable
        dep_repo_name = None
        if target_repo and target_repo.get("name"):
            dep_repo_name = target_repo["name"]

        if pr_info:
            logger.info("[%s] PR created: %s", ctx.todo_id, pr_info.get("url"))
            await ctx.db.execute(
                """
                INSERT INTO deliverables (
                    todo_id, sub_task_id, type, title,
                    pr_url, pr_number, branch_name, status, target_repo_name
                )
                VALUES ($1, $2, 'pull_request', $3, $4, $5, $6, 'pending', $7)
                """,
                ctx.todo_id,
                sub_task["id"],
                f"PR: {todo['title']}",
                pr_info.get("url"),
                pr_info.get("number"),
                branch_name,
                dep_repo_name,
            )
        else:
            logger.warning("[%s] create_pr returned empty result", ctx.todo_id)
            await ctx.post_system_message("**PR creation failed:** git provider returned no PR data.")

        return pr_info
    except Exception as exc:
        logger.error("[%s] Failed to finalize subtask workspace", ctx.todo_id, exc_info=True)
        err_detail = str(exc)[:500]
        await ctx.post_system_message(f"**PR creation failed:** {err_detail}")
        return None


# ──────────────────────────────────────────────────────────────────────
# Release subtask creation
# ──────────────────────────────────────────────────────────────────────

async def create_release_subtasks(
    ctx: HandlerContext, merge_subtask: dict, project_settings: dict,
) -> None:
    """Create chained release pipeline subtasks after a successful merge."""
    # Resolve per-repo release config (backwards compat: fall back to legacy single key)
    merge_st_target = merge_subtask.get("target_repo")
    if isinstance(merge_st_target, str):
        merge_st_target = json.loads(merge_st_target)
    _repo_name = (merge_st_target or {}).get("name", "main")
    release_configs = project_settings.get("release_configs", {})
    release_config = release_configs.get(_repo_name) or project_settings.get("release_config", {})
    if not release_config:
        logger.info("[%s] No release_config for repo '%s', skipping release subtasks", ctx.todo_id, _repo_name)
        return

    merge_st_id = str(merge_subtask["id"])
    target_repo_json = merge_subtask.get("target_repo")
    if isinstance(target_repo_json, str):
        target_repo_json = json.loads(target_repo_json)

    max_order = await ctx.db.fetchval(
        "SELECT COALESCE(MAX(execution_order), 0) FROM sub_tasks WHERE todo_id = $1",
        ctx.todo_id,
    )

    # 1. Build watcher subtask (depends on merge)
    build_row = await ctx.db.fetchrow(
        """
        INSERT INTO sub_tasks (
            todo_id, title, description, agent_role,
            execution_order, depends_on, target_repo
        )
        VALUES ($1, $2, $3, 'release_build_watcher', $4, $5, $6)
        RETURNING id
        """,
        ctx.todo_id,
        "Watch build pipeline",
        "Monitor CI/CD build pipeline after merge and capture build artifact hash.",
        max_order + 1,
        [merge_st_id],
        target_repo_json,
    )
    build_watcher_id = str(build_row["id"])
    logger.info("[%s] Created release_build_watcher sub-task %s (depends on merge %s)",
                 ctx.todo_id, build_watcher_id, merge_st_id)

    last_dep_id = build_watcher_id
    current_order = max_order + 2

    # 2. Test/staging deployer (if enabled)
    test_config = release_config.get("test_release", {})
    if test_config.get("enabled"):
        test_row = await ctx.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, target_repo
            )
            VALUES ($1, $2, $3, 'release_deployer', $4, $5, $6)
            RETURNING id
            """,
            ctx.todo_id,
            "Deploy to test/staging",
            "Deploy build artifact to test/staging environment. Environment: test",
            current_order,
            [last_dep_id],
            target_repo_json,
        )
        last_dep_id = str(test_row["id"])
        current_order += 1
        logger.info("[%s] Created release_deployer (test) sub-task %s", ctx.todo_id, last_dep_id)

    # 3. Prod deployer (if enabled)
    prod_config = release_config.get("prod_release", {})
    if prod_config.get("enabled"):
        await ctx.db.fetchrow(
            """
            INSERT INTO sub_tasks (
                todo_id, title, description, agent_role,
                execution_order, depends_on, target_repo
            )
            VALUES ($1, $2, $3, 'release_deployer', $4, $5, $6)
            RETURNING id
            """,
            ctx.todo_id,
            "Deploy to production",
            "Deploy build artifact to production environment. Environment: prod",
            current_order,
            [last_dep_id],
            target_repo_json,
        )
        logger.info("[%s] Created release_deployer (prod) sub-task %s", ctx.todo_id, last_dep_id)

    await ctx.post_system_message(
        "**Release pipeline:** Created release sub-tasks (build watcher"
        + (", test deploy" if test_config.get("enabled") else "")
        + (", prod deploy" if prod_config.get("enabled") else "")
        + ")."
    )


# ──────────────────────────────────────────────────────────────────────
# Post-merge builds
# ──────────────────────────────────────────────────────────────────────

async def create_pr_creator_subtask(
    ctx: HandlerContext, approved_st: dict, chain_id=None,
) -> None:
    """Create a pr_creator sub-task after reviewer approval."""
    depends_on: list[str] = []
    if chain_id:
        chain_tasks = await ctx.db.fetch(
            "SELECT id FROM sub_tasks WHERE todo_id = $1 AND "
            "(review_chain_id = $2 OR id = $2)",
            ctx.todo_id,
            chain_id,
        )
        depends_on = [str(t["id"]) for t in chain_tasks]

    approved_id = str(approved_st["id"])
    if approved_id not in depends_on:
        depends_on.append(approved_id)

    target_repo_json = approved_st.get("target_repo")
    if isinstance(target_repo_json, str):
        target_repo_json = json.loads(target_repo_json)

    row = await ctx.db.fetchrow(
        """
        INSERT INTO sub_tasks (
            todo_id, title, description, agent_role,
            execution_order, depends_on, review_chain_id, target_repo
        )
        VALUES ($1, $2, $3, 'pr_creator', $4, $5, $6, $7)
        RETURNING id
        """,
        ctx.todo_id,
        "Create Pull Request",
        "Commit all workspace changes, push to a feature branch, and create a pull request.",
        (approved_st.get("execution_order") or 0) + 1,
        depends_on,
        chain_id,
        target_repo_json,
    )
    logger.info("Created pr_creator sub-task %s for chain %s", row["id"], chain_id)
    await ctx.post_system_message(
        "**Review loop:** Reviewer approved. Created PR sub-task."
    )


# ──────────────────────────────────────────────────────────────────────
# Post-merge builds
# ──────────────────────────────────────────────────────────────────────

async def run_post_merge_builds(
    ctx: HandlerContext, todo: dict, build_commands: list[str], workspace_path: str,
) -> None:
    """Pull latest after merge and run build commands.

    workspace_path IS the git working directory.
    """
    repo_dir = workspace_path

    proc = await asyncio.create_subprocess_exec(
        "git", "pull", "origin",
        cwd=repo_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await proc.communicate()

    for cmd in build_commands:
        try:
            exit_code, output = await ctx.workspace_mgr.run_command(cmd, repo_dir, timeout=120)
            if exit_code != 0:
                await ctx.post_system_message(
                    f"**Post-merge build failed:** `{cmd}`\n```\n{output[:500]}\n```"
                )
            else:
                logger.info("Post-merge build passed: %s", cmd)
        except Exception as e:
            await ctx.post_system_message(
                f"**Post-merge build error:** `{cmd}` — {str(e)[:200]}"
            )
