"""Agent registry: single source of truth for all default agent definitions.

The coordinator, API, and dashboard all read from here instead of
maintaining their own copies of agent prompts, tool grants, and metadata.

Also contains the canonical BUILTIN_TOOLS registry — every tool's schema,
prompt description, and example live here.  Both the LLM tool-call API
and the system-prompt "Available Tools" block are generated from this
single source.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Builtin tool definitions ──────────────────────────────────────
#
# Each entry is the single source of truth for a workspace tool.
#  - name / description / input_schema → sent to the LLM via the tool-use API
#  - prompt_hint  → injected into the system prompt so every model (including
#                    those with weak native tool-calling) knows what's available
#  - example      → short usage example shown in the prompt


@dataclass(frozen=True)
class BuiltinToolDef:
    """Canonical definition of a built-in workspace tool."""

    name: str
    description: str
    input_schema: dict
    prompt_hint: str          # one-liner for the system-prompt block
    example: str = ""         # optional short example for the prompt


BUILTIN_TOOLS: dict[str, BuiltinToolDef] = {}


def _register_tool(defn: BuiltinToolDef) -> None:
    BUILTIN_TOOLS[defn.name] = defn


_register_tool(BuiltinToolDef(
    name="read_file",
    description="Read a file's contents. Path is relative to repo root.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to repo root"},
        },
        "required": ["path"],
    },
    prompt_hint="Read a file's contents. Args: path (relative to repo root)",
    example='read_file(path="src/main.py")',
))

_register_tool(BuiltinToolDef(
    name="write_file",
    description="Write content to a file. Creates parent directories. Path is relative to repo root.",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to repo root"},
            "content": {"type": "string", "description": "Full file content to write"},
        },
        "required": ["path", "content"],
    },
    prompt_hint="Write/overwrite a file (creates parent dirs). Args: path, content",
    example='write_file(path="src/utils.py", content="def helper(): ...")',
))

_register_tool(BuiltinToolDef(
    name="list_directory",
    description="List files and directories. Path is relative to repo root (empty for root).",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path relative to repo root"},
        },
        "required": ["path"],
    },
    prompt_hint="List files and directories. Args: path (empty string for repo root)",
    example='list_directory(path="src/")',
))

_register_tool(BuiltinToolDef(
    name="search_files",
    description=(
        "Search for a text pattern across files using grep. "
        "Returns matching lines with file paths and line numbers."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search pattern (regex supported)"},
            "path": {"type": "string", "description": "Directory to search in, relative to repo root (empty for root)"},
            "file_glob": {"type": "string", "description": "File glob filter, e.g. '*.py', '*.ts' (optional)"},
        },
        "required": ["pattern"],
    },
    prompt_hint="Grep for a pattern across files. Args: pattern, path (optional), file_glob (optional)",
    example='search_files(pattern="def handle_", file_glob="*.py")',
))

_register_tool(BuiltinToolDef(
    name="run_command",
    description="Run a shell command in the repo directory. Use for builds, tests, linting, git, gh CLI, etc.",
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
        },
        "required": ["command"],
    },
    prompt_hint="Run a shell command in the repo directory. Args: command",
    example='run_command(command="npm test")',
))


def get_builtin_tool_defs(role: str) -> list[BuiltinToolDef]:
    """Return the BuiltinToolDef objects granted to *role*."""
    names = get_default_tools(role)
    return [BUILTIN_TOOLS[n] for n in names if n in BUILTIN_TOOLS]


def get_builtin_tool_schemas(workspace_path: str, role: str) -> list[dict]:
    """Return LLM-API-ready tool dicts (name, description, input_schema) for a role.

    Adds workspace metadata so McpToolExecutor knows these are built-in.
    """
    meta = {"_builtin": True, "_workspace_path": workspace_path}
    return [
        {
            "name": td.name,
            "description": td.description,
            "input_schema": td.input_schema,
            **meta,
        }
        for td in get_builtin_tool_defs(role)
    ]


def build_tools_prompt_block(role: str) -> str:
    """Build the '## Available Tools' system-prompt section for a role.

    Generated from the same BUILTIN_TOOLS registry used for the API schemas,
    ensuring the prompt and API are always in sync.
    """
    defs = get_builtin_tool_defs(role)
    if not defs:
        return ""
    lines = ["\n\n## Available Tools"]
    for td in defs:
        lines.append(f"- `{td.name}`: {td.prompt_hint}")
        if td.example:
            lines.append(f"  Example: `{td.example}`")
    lines.append("")
    lines.append("You MUST call these tools to do your work. Do not just output code as text.")
    return "\n".join(lines)


# ── Agent definitions ─────────────────────────────────────────────


@dataclass(frozen=True)
class AgentDefinition:
    """Immutable descriptor for a default agent."""

    role: str
    display_name: str
    description: str
    system_prompt: str
    default_tools: list[str] = field(default_factory=list)
    default_model: str | None = None
    tool_rule_categories: list[str] = field(default_factory=lambda: ["general"])


# Populated by _register() calls below
AGENT_REGISTRY: dict[str, AgentDefinition] = {}


def _register(defn: AgentDefinition) -> None:
    AGENT_REGISTRY[defn.role] = defn


def get_agent_definition(role: str) -> AgentDefinition | None:
    return AGENT_REGISTRY.get(role)


def get_all_definitions() -> list[AgentDefinition]:
    return list(AGENT_REGISTRY.values())


def get_default_system_prompt(role: str) -> str:
    defn = AGENT_REGISTRY.get(role)
    return defn.system_prompt if defn else "You are a helpful AI assistant."


def get_default_tools(role: str) -> list[str]:
    defn = AGENT_REGISTRY.get(role)
    return list(defn.default_tools) if defn else ["read_file", "list_directory", "search_files"]


# ── Register all default agents ───────────────────────────────────

_register(AgentDefinition(
    role="coder",
    display_name="Coder",
    description="Writes code, implements features, and fixes bugs",
    system_prompt=(
        "You are a senior software engineer. You MUST use the provided tools to "
        "read and write files in the workspace. Do NOT just output code as text.\n\n"
        "## Workflow\n"
        "1. Use `read_file` and `list_directory` to understand the existing codebase\n"
        "2. Use `search_files` to find relevant patterns and usages\n"
        "3. Use `write_file` to create or modify files with your changes\n"
        "4. Use `run_command` to verify your changes (build, lint, test)\n\n"
        "## Rules\n"
        "- ALWAYS use `write_file` to apply changes — never just describe code in text\n"
        "- Read files before modifying them to understand the current state\n"
        "- Write complete file contents (not partial snippets)\n"
        "- Follow the project's existing patterns and conventions\n"
        "- Include necessary imports and error handling\n"
        "- Only add comments for non-obvious logic"
    ),
    default_tools=["read_file", "write_file", "list_directory", "search_files", "run_command"],
    tool_rule_categories=["coding", "quality", "general"],
))

_register(AgentDefinition(
    role="tester",
    display_name="Tester",
    description="Writes and runs tests, validates implementations",
    system_prompt=(
        "You are a test engineer. You MUST use the provided tools to read code and write tests.\n\n"
        "## Workflow\n"
        "1. Use `read_file` to understand the implementation being tested\n"
        "2. Use `search_files` to find existing test patterns\n"
        "3. Use `write_file` to create or update test files\n"
        "4. Use `run_command` to execute the tests and verify they pass\n\n"
        "## Rules\n"
        "- ALWAYS use `write_file` to create test files — never just output code as text\n"
        "- Cover happy paths, edge cases, and error conditions\n"
        "- Follow the project's existing test conventions and framework\n"
        "- Run tests after writing them to confirm they pass"
    ),
    default_tools=["read_file", "write_file", "list_directory", "search_files", "run_command"],
    tool_rule_categories=["testing", "quality", "general"],
))

_register(AgentDefinition(
    role="reviewer",
    display_name="Reviewer",
    description="Reviews code quality, suggests improvements",
    system_prompt=(
        "You are a code reviewer. You MUST use the provided tools to read and examine code.\n\n"
        "## Workflow\n"
        "1. Use `read_file` to examine the changed files\n"
        "2. Use `search_files` to understand how changes affect the rest of the codebase\n"
        "3. Use `run_command` to check build status, run linting, or run tests\n"
        "4. Use `run_command` with `git diff` to see exactly what changed\n\n"
        "## Rules\n"
        "- ALWAYS read the actual files before reviewing — never review blindly\n"
        "- Check for bugs, security issues, performance, and correctness\n"
        "- Be specific and constructive in your feedback\n"
        "- If the code works correctly and follows good practices, approve it"
    ),
    default_tools=["read_file", "list_directory", "search_files", "run_command"],
    tool_rule_categories=["review", "quality", "general"],
))

_register(AgentDefinition(
    role="pr_creator",
    display_name="PR Creator",
    description="Creates pull requests with proper descriptions",
    system_prompt=(
        "PR description writer. Create a clear PR description with summary, "
        "changes, testing done, and reviewer notes."
    ),
    default_tools=["read_file", "list_directory", "search_files"],
))

_register(AgentDefinition(
    role="report_writer",
    display_name="Report Writer",
    description="Generates documentation and reports",
    system_prompt=(
        "Technical writer. Create a clear, structured report with key findings "
        "and recommendations."
    ),
    default_tools=["read_file", "list_directory", "search_files"],
))

_register(AgentDefinition(
    role="merge_agent",
    display_name="Merge Agent",
    description="Merges approved PRs, checks CI status, and runs post-merge builds",
    system_prompt="Merge agent. Review CI status and merge approved pull requests.",
    default_tools=["read_file", "list_directory", "run_command"],
))

PLANNER_CHAT_PROMPT = """\
You are a project planner and assistant. You help users plan and manage their software projects.

TASK CREATION RULES:
1. When the user asks you to implement, build, fix, add, or change anything, create a task using action__create_task.
2. For well-understood tasks, include sub_tasks with agent roles (coder, tester, reviewer, etc.) \
— this sends the task directly to execution, skipping intake and planning.
3. For ambiguous or complex tasks that need clarification, create the task WITHOUT sub_tasks \
— it will go through the intake and planning pipeline.
4. When deleting a task, ALWAYS ask for confirmation first.

PLANNING GUIDELINES:
- Break work into focused sub-tasks: one for coding, one for testing, one for review
- Use agent roles: coder (implements), tester (writes tests), reviewer (reviews), pr_creator (creates PR)
- Set execution_order for sequential work (0 = parallel)
- Use depends_on (0-based indexes) for dependencies between sub-tasks

You also help with:
- Answering questions about the project, codebase, and architecture
- Debugging help and suggesting approaches
- Project analysis and insights

Be concise and helpful."""

_register(AgentDefinition(
    role="planner",
    display_name="Planner",
    description="Project chat agent — plans work, creates tasks, answers project questions",
    system_prompt=PLANNER_CHAT_PROMPT,
    default_tools=[],
    tool_rule_categories=["general"],
))
