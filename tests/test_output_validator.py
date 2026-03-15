"""Tests for the structured agent output validation system."""

import json

import pytest

from agents.orchestrator.output_validator import (
    MAX_VALIDATION_RETRIES,
    build_correction_prompt,
    build_structured_output_instruction,
    extract_json_from_content,
    validate_agent_output,
)
from agents.schemas.output import ROLE_OUTPUT_SCHEMAS


# ── JSON extraction ─────────────────────────────────────────────────


def test_extract_json_simple():
    text = 'Here is my output:\n{"approach": "did it", "files_changed": []}'
    result = extract_json_from_content(text)
    assert result is not None
    data = json.loads(result)
    assert data["approach"] == "did it"


def test_extract_json_markdown_fence():
    text = '```json\n{"verdict": "approved", "approved": true}\n```'
    result = extract_json_from_content(text)
    assert result is not None
    data = json.loads(result)
    assert data["verdict"] == "approved"


def test_extract_json_nested_braces():
    text = '{"issues": [{"severity": "minor", "description": "x"}]}'
    result = extract_json_from_content(text)
    data = json.loads(result)
    assert len(data["issues"]) == 1


def test_extract_json_none_when_missing():
    assert extract_json_from_content("No JSON here at all") is None
    assert extract_json_from_content("") is None


def test_extract_json_handles_strings_with_braces():
    text = '{"approach": "used dict[str, int] for mapping"}'
    result = extract_json_from_content(text)
    assert result is not None
    data = json.loads(result)
    assert "dict[str, int]" in data["approach"]


# ── Coder validation ────────────────────────────────────────────────


def test_valid_coder_output():
    content = (
        "I implemented the feature.\n\n"
        '```json\n'
        '{"approach": "Added auth middleware", "files_changed": ["src/auth.py"], "setup_steps": ["run migrate"]}\n'
        '```'
    )
    result, errors = validate_agent_output("coder", content)
    assert errors == []
    assert result is not None
    assert result["approach"] == "Added auth middleware"
    assert result["files_changed"] == ["src/auth.py"]
    assert result["setup_steps"] == ["run migrate"]
    assert result["content"] == content
    assert result["raw_content"] == content


def test_coder_missing_required_field():
    content = '{"files_changed": ["x.py"]}'
    result, errors = validate_agent_output("coder", content)
    assert result is None
    assert len(errors) > 0
    assert any("approach" in e for e in errors)


# ── Tester validation ───────────────────────────────────────────────


def test_valid_tester_output():
    content = json.dumps({
        "test_files": ["tests/test_auth.py"],
        "test_summary": "Added 5 tests for auth flow",
        "bug_reproduced_before_fix": True,
        "bug_resolved_after_fix": True,
    })
    result, errors = validate_agent_output("tester", content)
    assert errors == []
    assert result is not None
    assert result["bug_reproduced_before_fix"] is True
    assert result["bug_resolved_after_fix"] is True


def test_tester_defaults_for_optional_bools():
    content = json.dumps({
        "test_files": [],
        "test_summary": "Some tests",
    })
    result, errors = validate_agent_output("tester", content)
    assert errors == []
    assert result is not None
    assert result["bug_reproduced_before_fix"] is False
    assert result["bug_resolved_after_fix"] is False


# ── Reviewer validation ─────────────────────────────────────────────


def test_valid_reviewer_output():
    content = json.dumps({
        "verdict": "needs_changes",
        "approved": False,
        "matches_plan": False,
        "issues": [
            {"severity": "major", "description": "Missing error handling", "suggestion": "Add try/except"}
        ],
        "summary": "Code needs work",
    })
    result, errors = validate_agent_output("reviewer", content)
    assert errors == []
    assert result is not None
    assert result["verdict"] == "needs_changes"
    assert result["approved"] is False
    assert result["matches_plan"] is False
    assert len(result["issues"]) == 1
    assert result["issues"][0]["severity"] == "major"


def test_reviewer_missing_verdict():
    content = json.dumps({"approved": True, "summary": "Looks good"})
    result, errors = validate_agent_output("reviewer", content)
    assert result is None
    assert any("verdict" in e for e in errors)


# ── PR Creator validation ───────────────────────────────────────────


def test_valid_pr_creator_output():
    content = json.dumps({
        "pr_title": "feat: add auth",
        "pr_body": "## Summary\nAdded auth.",
        "breaking_changes": [],
    })
    result, errors = validate_agent_output("pr_creator", content)
    assert errors == []
    assert result["pr_title"] == "feat: add auth"


# ── Report Writer validation ────────────────────────────────────────


def test_valid_report_writer_output():
    content = json.dumps({
        "title": "Analysis Report",
        "executive_summary": "We analyzed the system.",
        "report_body": "## Findings\n...",
    })
    result, errors = validate_agent_output("report_writer", content)
    assert errors == []
    assert result["title"] == "Analysis Report"


# ── Merge Agent validation ──────────────────────────────────────────


def test_valid_merge_agent_output():
    content = json.dumps({
        "merge_decision": "merge",
        "reason": "CI passed, reviewer approved",
        "ci_passed": True,
    })
    result, errors = validate_agent_output("merge_agent", content)
    assert errors == []
    assert result["merge_decision"] == "merge"


# ── Edge cases ──────────────────────────────────────────────────────


def test_no_json_in_content():
    result, errors = validate_agent_output("coder", "I wrote some code but no JSON.")
    assert result is None
    assert len(errors) == 1
    assert "No JSON object found" in errors[0]


def test_malformed_json_trailing_comma():
    content = '{"approach": "fix it", "files_changed": ["a.py",], "setup_steps": []}'
    result, errors = validate_agent_output("coder", content)
    assert errors == []
    assert result is not None
    assert result["approach"] == "fix it"


def test_malformed_json_broken():
    content = '{"approach": "fix it", broken'
    result, errors = validate_agent_output("coder", content)
    assert result is None
    assert any("malformed" in e.lower() or "JSON" in e for e in errors)


def test_unknown_role_passthrough():
    content = "Just some text output from a custom agent."
    result, errors = validate_agent_output("custom_frontend_dev", content)
    assert errors == []
    assert result is not None
    assert result["content"] == content
    assert result["raw_content"] == content


def test_backward_compat_content_key():
    """Validated output always has 'content' key for backward compat."""
    content = json.dumps({
        "approach": "Did stuff",
        "files_changed": [],
        "setup_steps": [],
    })
    result, errors = validate_agent_output("coder", content)
    assert errors == []
    assert "content" in result
    assert result["content"] == content


# ── Correction prompt ───────────────────────────────────────────────


def test_correction_prompt_lists_errors():
    errors = ["Field 'verdict': Field required", "Field 'approved': Field required"]
    prompt = build_correction_prompt("reviewer", errors, "original text")
    assert "verdict" in prompt
    assert "approved" in prompt
    assert "did not pass output validation" in prompt
    assert "```json" in prompt


# ── Structured output instruction ───────────────────────────────────


def test_structured_output_instruction_contains_fields():
    instruction = build_structured_output_instruction("reviewer")
    assert "verdict" in instruction
    assert "approved" in instruction
    assert "matches_plan" in instruction
    assert "Required Structured Output" in instruction
    assert "```json" in instruction


def test_structured_output_instruction_empty_for_unknown():
    instruction = build_structured_output_instruction("custom_role_xyz")
    assert instruction == ""


def test_all_registered_roles_have_instructions():
    for role in ROLE_OUTPUT_SCHEMAS:
        instruction = build_structured_output_instruction(role)
        assert "Required Structured Output" in instruction, f"Missing instruction for {role}"


def test_max_validation_retries_is_reasonable():
    assert MAX_VALIDATION_RETRIES >= 1
    assert MAX_VALIDATION_RETRIES <= 5
