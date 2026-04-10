"""Settings migration and access helpers.

Handles migration of flat settings_json to the new namespaced format
and provides a uniform reader that works with both formats.

New namespaced structure:
    planning:   { require_approval, guidelines }
    execution:  { work_rules, architect_editor, max_iterations }
    git:        { merge_method, require_merge_approval, build_commands, post_merge_actions }
    debugging:  { log_sources, mcp_hints, custom_instructions }
    release:    { enabled, webhooks }
    understanding: { status, project, dependencies, linking }
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Sections that the new PUT /settings/{section} endpoint accepts
VALID_SECTIONS = frozenset({
    "planning", "execution", "git", "debugging", "release",
})


# ------------------------------------------------------------------
# Format detection
# ------------------------------------------------------------------

def is_new_format(settings: dict) -> bool:
    """Return True if settings_json is already in the new namespaced format."""
    return isinstance(settings.get("planning"), dict)


# ------------------------------------------------------------------
# Migration: old flat → new namespaced
# ------------------------------------------------------------------

def migrate_settings(settings: dict, *, project_row: dict | None = None) -> dict:
    """Migrate flat settings_json to namespaced format.

    Idempotent: if already in new format, returns as-is.
    ``project_row`` supplies column-level fields (architect_editor_enabled etc.)
    that used to live on the projects table directly.
    """
    if not settings:
        return _default_settings()

    if is_new_format(settings):
        return settings

    pr = project_row or {}

    new: dict[str, Any] = {
        "planning": {
            "require_approval": settings.get("require_plan_approval", False),
            "guidelines": settings.get("planning_guidelines", ""),
        },
        "execution": {
            "work_rules": settings.get("work_rules", {}),
            "architect_editor": {
                "enabled": pr.get("architect_editor_enabled", False),
                "architect_model": pr.get("architect_model"),
                "editor_model": pr.get("editor_model"),
            },
            "max_iterations": 500,
        },
        "git": {
            "merge_method": settings.get("merge_method", "squash"),
            "require_merge_approval": settings.get("require_merge_approval", False),
            "build_commands": _migrate_build_commands(settings.get("build_commands", [])),
            "post_merge_actions": settings.get("post_merge_actions", {}),
        },
        "debugging": settings.get("debug_context", {}),
        "release": {
            "enabled": settings.get("release_pipeline_enabled", False),
            "webhooks": _migrate_release_to_webhooks(settings),
        },
        "understanding": {
            "status": settings.get("analysis_status"),
            "project": settings.get("project_understanding", {}),
            "dependencies": settings.get("dep_understandings", {}),
            "linking": settings.get("linking_document", {}),
        },
    }

    # Preserve keys that don't belong to any section
    for key in ("index_metadata", "merge_pipelines",
                "release_configs", "release_config",
                "analysis_step", "analysis_detail"):
        if key in settings:
            new[key] = settings[key]

    return new


def _default_settings() -> dict:
    """Return an empty settings object in the new format."""
    return {
        "planning": {"require_approval": False, "guidelines": ""},
        "execution": {
            "work_rules": {},
            "architect_editor": {"enabled": False, "architect_model": None, "editor_model": None},
            "max_iterations": 500,
        },
        "git": {
            "merge_method": "squash",
            "require_merge_approval": False,
            "build_commands": [],
            "post_merge_actions": {},
        },
        "debugging": {},
        "release": {"enabled": False, "webhooks": []},
        "understanding": {"status": None, "project": {}, "dependencies": {}, "linking": {}},
    }


def _migrate_build_commands(cmds: list) -> list:
    """Convert string[] to structured build command objects."""
    if not cmds:
        return []
    result = []
    for cmd in cmds:
        if isinstance(cmd, str):
            result.append({"command": cmd, "description": "", "run_on": "quality_check"})
        elif isinstance(cmd, dict):
            result.append(cmd)
    return result


def _migrate_release_to_webhooks(settings: dict) -> list:
    """Convert old release_configs to webhook list (best-effort)."""
    # The old format had release_configs per-repo with build_config, test_release, prod_release.
    # The new format has a flat list of webhooks. This is a structural change so we
    # start with an empty list — the user can re-configure in the new UI.
    return []


# ------------------------------------------------------------------
# Setting reader — works with both old and new formats
# ------------------------------------------------------------------

def read_setting(settings: dict, new_path: str, old_key: str | None = None, default: Any = None) -> Any:
    """Read from new namespaced path first, fall back to old flat key.

    ``new_path`` is dot-delimited, e.g. ``"planning.require_approval"``.
    ``old_key`` is the legacy flat key, e.g. ``"require_plan_approval"``.
    """
    # Try new namespaced path
    val = _traverse(settings, new_path)
    if val is not None:
        return val

    # Fall back to old flat key
    if old_key is not None:
        val = settings.get(old_key)
        if val is not None:
            return val

    return default


def _traverse(d: dict, path: str) -> Any:
    """Walk a dot-delimited path through nested dicts. Returns None on miss."""
    parts = path.split(".")
    val: Any = d
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return None
    return val


# ------------------------------------------------------------------
# Build commands — normalize to string list for runners
# ------------------------------------------------------------------

def get_build_command_strings(settings: dict) -> list[str]:
    """Extract build commands as a flat list of command strings.

    Handles both old (list[str]) and new (list[dict]) formats.
    """
    raw = read_setting(settings, "git.build_commands", "build_commands", [])
    if not raw:
        return []
    result = []
    for item in raw:
        if isinstance(item, str):
            result.append(item)
        elif isinstance(item, dict) and item.get("command"):
            result.append(item["command"])
    return result


def get_build_commands_for_phase(settings: dict, phase: str = "quality_check") -> list[str]:
    """Extract build commands filtered by run_on phase.

    Falls back to returning all commands if they're in the old string format.
    """
    raw = read_setting(settings, "git.build_commands", "build_commands", [])
    if not raw:
        return []
    result = []
    for item in raw:
        if isinstance(item, str):
            # Old format — include for all phases
            result.append(item)
        elif isinstance(item, dict) and item.get("command"):
            if item.get("run_on", "quality_check") == phase:
                result.append(item["command"])
    return result


# ------------------------------------------------------------------
# Convenience: parse raw settings_json from DB
# ------------------------------------------------------------------

def parse_settings(raw: Any) -> dict:
    """Parse settings_json value from DB row (handles str or dict)."""
    if not raw:
        return {}
    if isinstance(raw, str):
        return json.loads(raw)
    if isinstance(raw, dict):
        return raw
    return {}
