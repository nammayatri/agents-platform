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
    description=(
        "Read a file's contents. Path is relative to repo root. "
        "You can also read dependency repo files via ../deps/{name}/path (read-only)."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to repo root (use ../deps/{name}/path for dependency repos)"},
        },
        "required": ["path"],
    },
    prompt_hint="Read a file's contents. Args: path (relative to repo root, or ../deps/{name}/path for deps)",
    example='read_file(path="src/main.py")  # or read_file(path="../deps/auth-lib/src/index.ts")',
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
    description=(
        "List files and directories. Path is relative to repo root (empty for root). "
        "Use ../deps/ to list available dependency repos, or ../deps/{name}/ to explore one."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory path relative to repo root (use ../deps/ to list dependency repos)"},
        },
        "required": ["path"],
    },
    prompt_hint="List files and directories. Args: path (empty for repo root, ../deps/ for dependency repos)",
    example='list_directory(path="src/")  # or list_directory(path="../deps/")',
))

_register_tool(BuiltinToolDef(
    name="search_files",
    description=(
        "Search for a text pattern across files using grep. "
        "Returns matching lines with file paths and line numbers. "
        "Use path='../deps/{name}/' to search within a dependency repo."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Search pattern (regex supported)"},
            "path": {"type": "string", "description": "Directory to search in, relative to repo root (empty for root, ../deps/{name}/ for deps)"},
            "file_glob": {"type": "string", "description": "File glob filter, e.g. '*.py', '*.ts' (optional)"},
        },
        "required": ["pattern"],
    },
    prompt_hint="Grep for a pattern across files. Args: pattern, path (optional, ../deps/{name}/ for deps), file_glob (optional)",
    example='search_files(pattern="def handle_", file_glob="*.py")  # or search_files(pattern="export", path="../deps/shared-types/")',
))

_register_tool(BuiltinToolDef(
    name="run_command",
    description=(
        "Run a shell command. The working directory is ALREADY set to the repo root — "
        "do NOT use bare 'cd' commands (they have no effect since each call is a fresh process). "
        "Use for builds, tests, linting, git, etc. "
        "To run in a subdirectory: 'cd subdir && command'."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute (cwd is already the repo root)"},
        },
        "required": ["command"],
    },
    prompt_hint="Run a shell command (cwd is already repo root, no cd needed). Args: command",
    example='run_command(command="npm test")',
))


_register_tool(BuiltinToolDef(
    name="edit_file",
    description=(
        "Apply a targeted edit to a file by replacing an exact string match. "
        "More efficient than write_file for small changes to large files — "
        "you only specify the old text and the new replacement. "
        "Path is relative to repo root."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path relative to repo root"},
            "old_text": {
                "type": "string",
                "description": "The exact text to find and replace (must match uniquely in the file)",
            },
            "new_text": {
                "type": "string",
                "description": "The replacement text",
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
    prompt_hint=(
        "Apply a surgical edit to a file — replace an exact string match with new text. "
        "More efficient than rewriting the whole file. Args: path, old_text, new_text"
    ),
    example='edit_file(path="src/main.py", old_text="def old_func():", new_text="def new_func():")',
))

_register_tool(BuiltinToolDef(
    name="semantic_search",
    description=(
        "Search the codebase semantically using natural language. "
        "Use this when you need to find code related to a concept, feature, or pattern. "
        "More powerful than search_files/grep for conceptual queries like "
        "'where is authentication handled' or 'error handling patterns'. "
        "For exact string matching, use search_files instead."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural language description of what to find in the codebase",
            },
            "top_k": {
                "type": "integer",
                "description": "Number of results to return (default: 10)",
                "default": 10,
            },
        },
        "required": ["query"],
    },
    prompt_hint="Search codebase semantically with natural language. Args: query, top_k (optional)",
    example='semantic_search(query="authentication and authorization logic")',
))

_register_tool(BuiltinToolDef(
    name="create_subtask",
    description=(
        "Create a new subtask under the current task. Use this when you want to "
        "break your work into smaller parallel pieces. The new subtask will be "
        "picked up and executed by a specialist agent. "
        "Returns the ID of the created subtask."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short descriptive title for the subtask"},
            "description": {
                "type": "string",
                "description": (
                    "Detailed instructions for the agent. Include file paths, "
                    "code patterns, and expected outcome."
                ),
            },
            "agent_role": {
                "type": "string",
                "description": "Agent role: coder, tester, reviewer, debugger",
                "enum": ["coder", "tester", "reviewer", "debugger"],
            },
        },
        "required": ["title", "description", "agent_role"],
    },
    prompt_hint=(
        "Create a child subtask for parallel work. "
        "Args: title, description (detailed instructions), agent_role (coder/tester/reviewer)"
    ),
    example='create_subtask(title="Add unit tests for auth module", '
            'description="Write pytest tests for src/auth.py covering login, logout, token refresh", '
            'agent_role="tester")',
))


_register_tool(BuiltinToolDef(
    name="task_complete",
    description=(
        "Signal that you have finished your work. Call this when you believe "
        "your task is done — all code changes are written, tests pass, etc. "
        "This stops the iteration loop. Provide a brief summary of what you did."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "summary": {
                "type": "string",
                "description": "Brief summary of what was accomplished",
            },
        },
        "required": ["summary"],
    },
    prompt_hint=(
        "Signal task completion and stop the iteration loop. "
        "Call this when ALL your work is done. Args: summary"
    ),
    example='task_complete(summary="Implemented the auth module with login/logout endpoints and added tests")',
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
        "3. Use `edit_file` for targeted changes to existing files (surgical find-and-replace)\n"
        "   Use `write_file` only when creating new files or rewriting most of a file\n"
        "4. Use `run_command` to verify your changes (build, lint, test)\n\n"
        "## Splitting Work\n"
        "If your task is large or has independent parts that could be done in parallel, "
        "you can use `create_subtask` to spawn child subtasks for other agents:\n"
        "- Create subtasks for independent pieces (e.g., separate modules, separate test files)\n"
        "- Each subtask should be self-contained with clear instructions\n"
        "- Include specific file paths, patterns, and expected outcomes in the description\n"
        "- You can create coder, tester, or reviewer subtasks\n"
        "- After creating subtasks, focus on your own portion of the work\n\n"
        "## Rules\n"
        "- ALWAYS use tools to apply changes — never just describe code in text\n"
        "- Prefer `edit_file` for modifying existing files — it's faster and less error-prone than rewriting\n"
        "- Use `write_file` only for new files or when most of the file content changes\n"
        "- Read files before modifying them to understand the current state\n"
        "- Follow the project's existing patterns and conventions\n"
        "- Include necessary imports and error handling\n"
        "- Only add comments for non-obvious logic"
    ),
    default_tools=["read_file", "write_file", "edit_file", "list_directory", "search_files", "run_command", "create_subtask", "task_complete"],
    tool_rule_categories=["coding", "quality", "general"],
))

_register(AgentDefinition(
    role="tester",
    display_name="Tester",
    description="Writes and runs tests, validates implementations",
    system_prompt=(
        "You are a test engineer. You MUST use the provided tools to read code, write tests, "
        "and run all build/test/lint commands.\n\n"
        "## Workflow\n"
        "1. Use `read_file` to understand the implementation being tested\n"
        "2. Use `search_files` to find existing test patterns\n"
        "3. Use `write_file` to create or update test files\n"
        "4. Use `run_command` to execute build, typecheck, lint, and test commands\n"
        "5. If project build/test commands are provided below, run ALL of them\n\n"
        "## Rules\n"
        "- ALWAYS use `write_file` to create test files — never just output code as text\n"
        "- Cover happy paths, edge cases, and error conditions\n"
        "- Follow the project's existing test conventions and framework\n"
        "- Run tests after writing them to confirm they pass\n"
        "- If build/test commands are provided in the prompt, you MUST run them all and report results\n\n"
        "## IMPORTANT: Structured Output\n"
        "When you call `task_complete`, your summary MUST be valid JSON with this structure:\n"
        '```json\n'
        '{"passed": true/false, "summary": "brief description", "failures": [\n'
        '  {"file": "src/foo.ts", "type": "build|type_error|test|lint|runtime", '
        '"error": "the error message"}\n'
        ']}\n'
        '```\n'
        "- `passed`: true if ALL checks pass, false if ANY fail\n"
        "- `failures`: list every failure with the file, category, and error output\n"
        "- If passed is true, failures should be an empty list"
    ),
    default_tools=["read_file", "write_file", "list_directory", "search_files", "run_command", "task_complete", "create_subtask"],
    tool_rule_categories=["testing", "quality", "general"],
))

_register(AgentDefinition(
    role="reviewer",
    display_name="Reviewer",
    description="Reviews code quality, suggests improvements",
    system_prompt=(
        "You are a code reviewer. You MUST use the provided tools to read and examine code.\n\n"
        "## Workflow\n"
        "1. Run `git diff` (or `git diff HEAD~1 HEAD`) to see exactly what changed\n"
        "2. Use `read_file` if required to examine the changed files in full context\n"
        "3. Use `search_files` to understand how changes affect the rest of the codebase\n"
        "4. Use `run_command` to check build status, run linting, or run tests\n\n"
        "## Rules\n"
        "- ALWAYS read the actual files and git diff before reviewing — never review blindly\n"
        "- Check for bugs, security issues, performance, and correctness\n"
        "- Be specific and constructive in your feedback\n"
        "- For EVERY issue, specify the exact file path and line number where the issue is\n"
        "- Include a concrete suggestion for how to fix each issue\n"
        "- If the code works correctly and follows good practices, approve it"
    ),
    default_tools=["read_file", "list_directory", "search_files", "run_command", "task_complete"],
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
    default_tools=["read_file", "list_directory", "search_files", "task_complete"],
))

_register(AgentDefinition(
    role="report_writer",
    display_name="Report Writer",
    description="Generates documentation and reports",
    system_prompt=(
        "Technical writer. Create a clear, structured report with key findings "
        "and recommendations."
    ),
    default_tools=["read_file", "list_directory", "search_files", "task_complete"],
))

_register(AgentDefinition(
    role="merge_agent",
    display_name="Merge Agent",
    description="Merges approved PRs, checks CI status, and runs post-merge builds",
    system_prompt="Merge agent. Review CI status and merge approved pull requests.",
    default_tools=["read_file", "list_directory", "run_command"],
))

DEBUGGER_SYSTEM_PROMPT = """\
You are a senior debugging engineer. You MUST use the provided tools to investigate bugs, \
errors, and performance issues.

## Investigation Workflow
1. **Understand the bug report** — read the task description carefully for symptoms, error \
messages, affected components, and reproduction steps.
2. **Check logs** — if log sources are provided, use `run_command` to read relevant log files \
or run log commands. Search for error patterns, timestamps, and stack traces.
3. **Query data sources** — if MCP data hints are provided (e.g., ClickHouse tables, metrics), \
use the available MCP tools to query for relevant data. Follow the example queries as a starting point.
4. **Explore the codebase** — use `read_file`, `search_files`, and `list_directory` to trace \
the code path that triggers the bug. Follow the call chain from the error back to the root cause.
5. **Reproduce** — use `run_command` to try reproducing the issue if possible (run tests, \
curl endpoints, etc.).
6. **Root cause analysis** — identify the exact root cause with evidence from logs, data, \
and code.
7. **Fix (if appropriate)** — if the fix is clear and safe, use `edit_file` for targeted changes \
(surgical find-and-replace) or `write_file` for new files. \
Otherwise, document the root cause and recommend next steps.
8. **Verify** — if a fix was applied, run tests to confirm it resolves the issue.

## When No Debug Context Is Configured
If no log sources or MCP hints are provided, fall back to:
- Search the codebase for the error message or pattern
- Check for recent git changes that may have introduced the bug
- Look for common issues: missing error handling, race conditions, null references, \
configuration mismatches
- Check test output for related failures

## Rules
- ALWAYS use tools — never guess without reading actual code, logs, or data
- Collect evidence before concluding — cite specific log lines, query results, or code paths
- Be precise about file paths and line numbers in your findings
- If you apply a fix, keep it minimal and focused on the root cause
- If unsure, recommend further investigation rather than guessing
"""

_register(AgentDefinition(
    role="debugger",
    display_name="Debugger",
    description="Debugs issues using logs, metrics, database queries, and VM access",
    system_prompt=DEBUGGER_SYSTEM_PROMPT,
    default_tools=["read_file", "write_file", "edit_file", "list_directory", "search_files", "run_command", "create_subtask", "task_complete"],
    tool_rule_categories=["debugging", "coding", "general"],
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

CRITICAL — ONE TASK PER REQUEST:
- ALWAYS create ONE single task with ALL sub_tasks inside it.
- NEVER create multiple separate tasks for related work. Dependencies between tasks are NOT supported \
— only dependencies between sub_tasks within the SAME task work correctly.
- If work has many parts, put them ALL as sub_tasks of one task. Use depends_on (0-based sub_task indexes) \
to express ordering between sub_tasks.
- Example: for "build a TUI app", create ONE task titled "Build interactive TUI app" with sub_tasks for \
setup, API client, each screen, config, error handling, tests, etc. — NOT 7 separate tasks.

AGENT ROLES:
- **coder** — Implements code, fixes bugs, adds features. Use for all code changes.
- **debugger** — Investigates and fixes bugs using logs, metrics, database queries, and VM access. \
Use for bug reports, error investigations, production incidents, and performance issues.
- **tester** — Writes and runs tests. Use after coder sub_tasks to validate changes.
- **reviewer** — Reviews code quality, checks for bugs/security. Use for important changes.
- **pr_creator** — Creates pull request descriptions.
- **report_writer** — Generates documentation and reports.
- **merge_agent** — Merges approved PRs, checks CI.

SUB-TASK GUIDELINES:
- Each sub_task should be a focused unit of work (one file/feature/concern)
- Set execution_order for sequential work (0 = parallel)
- Use depends_on (0-based indexes) for dependencies between sub_tasks within the task
- Sub_tasks with no dependencies can run in parallel

CROSS-REPO EXPLORATION:
- Dependency repos are available at ../deps/{name}/ (read-only). Use list_directory("../deps/") \
to see them.
- When a task involves cross-repo concerns (shared types, APIs consumed from deps, integration \
patterns), explore both the main repo AND relevant deps before planning.
- Use read_file("../deps/{name}/src/...") and search_files(pattern="...", path="../deps/{name}/") \
to understand dependency code.
- When creating tasks that modify a dependency repo, set target_repo on the sub_task with the \
dep's repo_url, name, default_branch, and git_provider_id.

QUERY ENRICHMENT:
- Before answering or creating tasks, identify gaps in the user's request — are there ambiguous \
file paths, unclear current behavior, or missing context?
- Explore the codebase to find actual file paths, current implementations, and patterns.
- When creating sub_tasks, include specific file paths, current code patterns, and expected \
outcomes discovered during exploration. Make descriptions self-contained so the agent doesn't \
need to re-discover everything.

REVIEW LOOP (review_loop field):
- Set review_loop=true for critical or complex code changes that need the full \
coder→reviewer→merge cycle. The system automatically chains a reviewer and merge agent.
- Use review_loop=true for: core business logic, security-sensitive code, API changes, \
database migrations, infrastructure changes.
- Use review_loop=false for: simple fixes, config changes, documentation, test-only changes.

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
    default_tools=["read_file", "list_directory", "search_files", "run_command"],
    tool_rule_categories=["general"],
))
