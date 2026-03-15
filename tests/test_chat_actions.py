"""Tests for the scoped chat tool registry."""

import json

import pytest

from agents.api.chat_actions import (
    execute_action,
    get_action_handler,
    get_actions_as_tools,
    is_action_tool,
)


def test_project_scope_returns_tools():
    """Project scope should return create_task and update_work_rules."""
    tools = get_actions_as_tools("project")
    names = {t["name"] for t in tools}
    assert "action__create_task" in names
    assert "action__update_work_rules" in names


def test_agent_scope_returns_no_project_tools():
    """Agent scope should NOT return project-scoped tools."""
    tools = get_actions_as_tools("agent")
    names = {t["name"] for t in tools}
    assert "action__create_task" not in names
    assert "action__update_work_rules" not in names


def test_task_scope_returns_no_project_tools():
    """Task scope should NOT return project-scoped tools."""
    tools = get_actions_as_tools("task")
    names = {t["name"] for t in tools}
    assert "action__create_task" not in names


def test_tool_definitions_have_valid_schemas():
    """All registered tools should have required schema fields."""
    for scope in ("project", "agent", "task"):
        tools = get_actions_as_tools(scope)
        for tool in tools:
            assert "name" in tool
            assert "description" in tool
            assert "input_schema" in tool
            assert tool["name"].startswith("action__")
            schema = tool["input_schema"]
            assert "type" in schema
            assert schema["type"] == "object"


def test_handler_lookup():
    """get_action_handler should find registered handlers."""
    handler = get_action_handler("action__create_task")
    assert handler is not None
    assert callable(handler)

    handler = get_action_handler("action__nonexistent")
    assert handler is None


def test_is_action_tool():
    """is_action_tool identifies action__ prefixed names."""
    assert is_action_tool("action__create_task") is True
    assert is_action_tool("action__update_work_rules") is True
    assert is_action_tool("mcp_some_tool") is False
    assert is_action_tool("create_task") is False


@pytest.mark.asyncio
async def test_execute_unknown_action():
    """Executing an unknown action returns an error JSON."""
    result = await execute_action("action__nonexistent", {}, {})
    data = json.loads(result)
    assert "error" in data
