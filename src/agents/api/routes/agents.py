"""Custom agent configurations: CRUD + AI chat builder."""

import json
import logging
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agents.agents.registry import get_agent_definition, get_all_definitions
from agents.api.deps import DB, CurrentUser

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────

class AgentInput(BaseModel):
    name: str
    role: str
    description: str | None = None
    system_prompt: str
    model_preference: str | None = None
    tools_enabled: list[str] = []


class AgentUpdate(BaseModel):
    name: str | None = None
    role: str | None = None
    description: str | None = None
    system_prompt: str | None = None
    model_preference: str | None = None
    tools_enabled: list[str] | None = None
    is_active: bool | None = None


class AgentChatInput(BaseModel):
    content: str


# ── CRUD ─────────────────────────────────────────────────────────────

@router.get("/agents")
async def list_agents(user: CurrentUser, db: DB):
    """List all agent configs: defaults from registry + user overrides + custom agents."""
    # Build defaults from registry
    defaults = []
    for defn in get_all_definitions():
        defaults.append({
            "role": defn.role,
            "name": defn.display_name,
            "description": defn.description,
            "system_prompt": defn.system_prompt,
            "default_tools": list(defn.default_tools),
            "default_model": defn.default_model,
            "is_default": True,
        })

    # Load all user agent configs
    rows = await db.fetch(
        "SELECT * FROM agent_configs WHERE owner_id = $1 ORDER BY created_at ASC",
        user["id"],
    )

    # Separate overrides (matching default roles) from truly custom agents
    default_roles = {d.role for d in get_all_definitions()}
    overrides: dict[str, dict] = {}
    custom: list[dict] = []
    for r in rows:
        row = dict(r)
        if row["role"] in default_roles:
            overrides[row["role"]] = row
        else:
            custom.append(row)

    return {
        "defaults": defaults,
        "overrides": overrides,
        "custom": custom,
    }


@router.get("/agents/tools")
async def list_available_tools(user: CurrentUser, db: DB):
    """Return all available tool names (builtin + MCP) for the tool picker UI."""
    builtin = [
        {"name": "read_file", "description": "Read a file's contents", "category": "builtin"},
        {"name": "write_file", "description": "Write content to a file", "category": "builtin"},
        {"name": "list_directory", "description": "List files and directories", "category": "builtin"},
        {"name": "search_files", "description": "Search for text patterns across files", "category": "builtin"},
        {"name": "run_command", "description": "Run a shell command in the repo", "category": "builtin"},
    ]

    # MCP tools from user's active servers
    mcp_servers = await db.fetch(
        "SELECT id, name, tools_json FROM mcp_servers WHERE owner_id = $1 AND is_active = TRUE",
        user["id"],
    )
    mcp: list[dict] = []
    for srv in mcp_servers:
        raw = srv["tools_json"]
        if not raw:
            continue
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(raw, list):
            for t in raw:
                mcp.append({
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "category": "mcp",
                    "server_name": srv["name"],
                })

    return {"builtin": builtin, "mcp": mcp}


@router.post("/agents", status_code=status.HTTP_201_CREATED)
async def create_agent(body: AgentInput, user: CurrentUser, db: DB):
    row = await db.fetchrow(
        """
        INSERT INTO agent_configs (owner_id, name, role, description, system_prompt, model_preference, tools_enabled)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *
        """,
        user["id"],
        body.name,
        body.role,
        body.description,
        body.system_prompt,
        body.model_preference,
        body.tools_enabled,
    )
    return dict(row)


@router.post("/agents/defaults/{role}/override", status_code=status.HTTP_201_CREATED)
async def create_default_override(role: str, body: AgentUpdate, user: CurrentUser, db: DB):
    """Create a user override for a default agent, pre-populated with defaults."""
    defn = get_agent_definition(role)
    if not defn:
        raise HTTPException(status_code=404, detail=f"No default agent with role '{role}'")

    existing = await db.fetchrow(
        "SELECT id FROM agent_configs WHERE owner_id = $1 AND role = $2",
        user["id"], role,
    )
    if existing:
        raise HTTPException(status_code=409, detail="Override already exists. Use PUT to update.")

    row = await db.fetchrow(
        """
        INSERT INTO agent_configs (owner_id, name, role, description, system_prompt, model_preference, tools_enabled)
        VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *
        """,
        user["id"],
        body.name or defn.display_name,
        role,
        body.description or defn.description,
        body.system_prompt or defn.system_prompt,
        body.model_preference,
        body.tools_enabled if body.tools_enabled is not None else list(defn.default_tools),
    )
    return dict(row)


# ── Agent Builder Chat (must come before /{agent_id} routes) ───────

AGENT_BUILDER_TOOLS = [
    {
        "name": "create_agent",
        "description": "Create a new custom agent with a specific role, name, and system prompt.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Display name for the agent (e.g. 'Frontend Developer')"},
                "role": {"type": "string", "description": "Unique role identifier, snake_case (e.g. 'frontend_dev')"},
                "description": {"type": "string", "description": "Short description of what this agent does"},
                "system_prompt": {"type": "string", "description": "The full system prompt / instructions for this agent. Be thorough and specific about the agent's expertise, coding style, and responsibilities."},
                "tools_enabled": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of tool names: read_file, write_file, list_directory, search_files, run_command (builtin), plus any MCP tool names",
                },
            },
            "required": ["name", "role", "system_prompt"],
        },
    },
    {
        "name": "update_agent",
        "description": "Update an existing custom agent's configuration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string", "description": "The UUID of the agent to update"},
                "name": {"type": "string"},
                "description": {"type": "string"},
                "system_prompt": {"type": "string"},
                "tools_enabled": {"type": "array", "items": {"type": "string"}},
                "is_active": {"type": "boolean"},
            },
            "required": ["agent_id"],
        },
    },
    {
        "name": "list_agents",
        "description": "List all current agent configurations (default + custom).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]

AGENT_BUILDER_SYSTEM = """\
You are an AI Agent Builder assistant. You help users create and configure custom AI agents \
for their agent orchestration platform.

When a user describes what kind of agent they want, use the `create_agent` tool to build it. \
Write thorough, specific system prompts that define the agent's:
- Expertise and domain knowledge
- Coding style and conventions
- Specific responsibilities and boundaries
- How it should interact with other agents in the team

Available builtin tools agents can use: read_file, write_file, list_directory, search_files, run_command.

Available tools: create_agent, update_agent, list_agents.

Keep responses concise. When creating an agent, explain what you built and why.
"""


@router.get("/agents/chat")
async def get_agent_chat(user: CurrentUser, db: DB):
    """Get agent builder chat history."""
    rows = await db.fetch(
        "SELECT * FROM agent_chat_messages WHERE user_id = $1 ORDER BY created_at ASC LIMIT 100",
        user["id"],
    )
    return [dict(r) for r in rows]


@router.delete("/agents/chat")
async def clear_agent_chat(user: CurrentUser, db: DB):
    """Clear agent builder chat history."""
    await db.execute("DELETE FROM agent_chat_messages WHERE user_id = $1", user["id"])
    return {"status": "cleared"}


@router.post("/agents/chat")
async def send_agent_chat(body: AgentChatInput, user: CurrentUser, db: DB):
    """Send a message to the agent builder AI. The AI has tools to create/update agents."""
    from agents.providers.registry import ProviderRegistry
    from agents.schemas.agent import LLMMessage

    # Store user message
    user_msg = await db.fetchrow(
        "INSERT INTO agent_chat_messages (user_id, role, content) VALUES ($1, 'user', $2) RETURNING *",
        user["id"],
        body.content,
    )

    try:
        # Get provider
        row = await db.fetchrow(
            "SELECT id FROM ai_provider_configs WHERE owner_id = $1 AND is_active = TRUE "
            "ORDER BY created_at ASC LIMIT 1",
            user["id"],
        )
        if not row:
            row = await db.fetchrow(
                "SELECT id FROM ai_provider_configs WHERE owner_id IS NULL AND is_active = TRUE "
                "ORDER BY created_at ASC LIMIT 1"
            )
        if not row:
            raise ValueError("No AI provider configured")

        registry = ProviderRegistry(db)
        provider = await registry.instantiate(str(row["id"]))

        # Load existing custom agents for context
        custom_agents = await db.fetch(
            "SELECT name, role, description FROM agent_configs WHERE owner_id = $1",
            user["id"],
        )
        agents_ctx = ""
        if custom_agents:
            lines = [f"  - {a['name']} ({a['role']}): {a['description'] or 'No description'}" for a in custom_agents]
            agents_ctx = "\n\nExisting custom agents:\n" + "\n".join(lines)

        # Load chat history
        history = await db.fetch(
            "SELECT role, content FROM agent_chat_messages WHERE user_id = $1 "
            "ORDER BY created_at DESC LIMIT 30",
            user["id"],
        )
        history = list(reversed(history))

        system = AGENT_BUILDER_SYSTEM + agents_ctx

        messages = [LLMMessage(role="system", content=system)]
        for row_h in history[:-1]:
            messages.append(LLMMessage(role=row_h["role"], content=row_h["content"]))
        messages.append(LLMMessage(role="user", content=body.content))

        from agents.providers.base import run_tool_loop

        metadata = None

        async def _execute_tool(name: str, arguments: dict) -> str:
            nonlocal metadata
            result = await _execute_agent_tool(name, arguments, user["id"], db)
            if result.get("metadata"):
                metadata = result["metadata"]
            return result["text"]

        content, response = await run_tool_loop(
            provider, messages,
            tools=AGENT_BUILDER_TOOLS,
            tool_executor=_execute_tool,
            max_rounds=5,
        )

        # Store assistant message
        assistant_msg = await db.fetchrow(
            """
            INSERT INTO agent_chat_messages (user_id, role, content, metadata_json)
            VALUES ($1, 'assistant', $2, $3::jsonb) RETURNING *
            """,
            user["id"],
            content,
            json.dumps(metadata) if metadata else None,
        )

        return {
            "user_message": dict(user_msg),
            "assistant_message": dict(assistant_msg),
        }

    except Exception as e:
        logger.error("Agent chat error: %s", e)
        err_msg = await db.fetchrow(
            "INSERT INTO agent_chat_messages (user_id, role, content) VALUES ($1, 'system', $2) RETURNING *",
            user["id"],
            f"Error: {str(e)}",
        )
        return {
            "user_message": dict(user_msg),
            "assistant_message": dict(err_msg),
        }


async def _execute_agent_tool(
    tool_name: str,
    tool_input: dict,
    user_id: str,
    db,
) -> dict:
    """Execute an agent builder tool and return the result."""
    if tool_name == "create_agent":
        row = await db.fetchrow(
            """
            INSERT INTO agent_configs (owner_id, name, role, description, system_prompt, tools_enabled)
            VALUES ($1, $2, $3, $4, $5, $6) RETURNING *
            """,
            user_id,
            tool_input["name"],
            tool_input["role"],
            tool_input.get("description", ""),
            tool_input["system_prompt"],
            tool_input.get("tools_enabled", []),
        )
        return {
            "text": json.dumps({
                "status": "created",
                "agent_id": str(row["id"]),
                "name": row["name"],
                "role": row["role"],
            }),
            "metadata": {
                "action": "agent_created",
                "agent_id": str(row["id"]),
                "agent_name": row["name"],
            },
        }

    elif tool_name == "update_agent":
        agent_id = tool_input.pop("agent_id", None)
        if not agent_id:
            return {"text": json.dumps({"error": "agent_id is required"})}

        existing = await db.fetchrow("SELECT * FROM agent_configs WHERE id = $1", agent_id)
        if not existing:
            return {"text": json.dumps({"error": "Agent not found"})}
        if str(existing["owner_id"]) != str(user_id):
            return {"text": json.dumps({"error": "Access denied"})}

        ALLOWED_AGENT_UPDATE_COLS = {"name", "description", "system_prompt", "model_preference", "tools_enabled", "is_active"}
        updates = {k: v for k, v in tool_input.items() if v is not None and k in ALLOWED_AGENT_UPDATE_COLS}
        if updates:
            set_parts = []
            values = []
            for i, (k, v) in enumerate(updates.items()):
                set_parts.append(f"{k} = ${i+2}")
                values.append(v)
            await db.execute(
                f"UPDATE agent_configs SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = $1",
                agent_id,
                *values,
            )

        return {
            "text": json.dumps({"status": "updated", "agent_id": agent_id}),
            "metadata": {"action": "agent_updated", "agent_id": agent_id},
        }

    elif tool_name == "list_agents":
        defaults_data = [
            {"role": d.role, "name": d.display_name, "description": d.description}
            for d in get_all_definitions()
        ]
        custom = await db.fetch(
            "SELECT id, name, role, description, is_active FROM agent_configs WHERE owner_id = $1",
            user_id,
        )
        return {
            "text": json.dumps({
                "defaults": defaults_data,
                "custom": [dict(r) for r in custom],
            }, default=str),
        }

    return {"text": json.dumps({"error": f"Unknown tool: {tool_name}"})}


# ── Agent CRUD by ID (after /chat routes to avoid path conflicts) ──

@router.put("/agents/{agent_id}")
async def update_agent(agent_id: str, body: AgentUpdate, user: CurrentUser, db: DB):
    existing = await db.fetchrow("SELECT * FROM agent_configs WHERE id = $1", agent_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Agent not found")
    if str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    ALLOWED_AGENT_UPDATE_COLS = {"name", "description", "system_prompt", "model_preference", "tools_enabled", "is_active"}
    updates = {k: v for k, v in body.model_dump().items() if v is not None and k in ALLOWED_AGENT_UPDATE_COLS}
    if not updates:
        return dict(existing)

    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items()):
        set_parts.append(f"{k} = ${i+2}")
        values.append(v)

    row = await db.fetchrow(
        f"UPDATE agent_configs SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = $1 RETURNING *",
        agent_id,
        *values,
    )
    return dict(row)


@router.delete("/agents/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(agent_id: str, user: CurrentUser, db: DB):
    existing = await db.fetchrow("SELECT owner_id FROM agent_configs WHERE id = $1", agent_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Agent not found")
    if str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")
    await db.execute("DELETE FROM agent_configs WHERE id = $1", agent_id)
