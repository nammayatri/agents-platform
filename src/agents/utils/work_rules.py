"""Work rules utilities — pure functions for resolving, filtering, and formatting.

Work rules are project-level instructions that constrain how AI agents behave.
Categories: coding, testing, review, quality (shell commands), general.

This module is the single canonical source for all work rules logic.
"""

from __future__ import annotations

import json


def resolve_work_rules(
    todo: dict,
    project: dict | None = None,
) -> dict:
    """Merge project-level work rules with per-task overrides.

    Pure computation — no I/O. Expects pre-loaded todo and project rows.

    Parameters
    ----------
    todo : dict
        The todo_items row. May contain ``rules_override_json``.
    project : dict | None
        The projects row. Work rules live in ``settings_json.work_rules``.

    Returns
    -------
    dict
        Merged rules: ``{category: [rule_strings]}``.
    """
    from agents.utils.settings_helpers import parse_settings, read_setting
    project_settings = parse_settings((project or {}).get("settings_json"))
    rules = dict(read_setting(project_settings, "execution.work_rules", "work_rules", {}))

    overrides = todo.get("rules_override_json") or {}
    if isinstance(overrides, str):
        overrides = json.loads(overrides)
    for category, values in overrides.items():
        rules[category] = values

    return rules


def filter_rules_for_role(work_rules: dict, role: str) -> dict:
    """Return only the rule categories relevant to the given agent role.

    Uses the ``tool_rule_categories`` from the agent definition registry.
    Falls back to ``["general"]`` if the role has no definition.
    """
    from agents.agents.registry import get_agent_definition

    defn = get_agent_definition(role)
    categories = defn.tool_rule_categories if defn else ["general"]
    return {cat: work_rules[cat] for cat in categories if cat in work_rules}


def format_rules_for_prompt(rules: dict) -> str:
    """Format work rules into a markdown block for LLM system prompt injection.

    Returns an empty string if no rules are provided.
    """
    if not rules:
        return ""
    parts = ["\n\n## Work Rules (you MUST follow these)\n"]
    for category, items in rules.items():
        if items:
            parts.append(f"### {category.title()}")
            for item in items:
                parts.append(f"- {item}")
    return "\n".join(parts)
