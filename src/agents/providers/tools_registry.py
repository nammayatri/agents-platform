"""Tools registry: resolves enabled MCP tools and skills for a project.

Queries MCP servers and skills owned by the user, filters out those
disabled for the specific project, and returns tool definitions ready
for the provider's `tools` parameter.
"""

import json
import logging

import asyncpg

logger = logging.getLogger(__name__)


class ToolsRegistry:
    def __init__(self, db: asyncpg.Pool):
        self.db = db

    async def resolve_tools(
        self,
        *,
        project_id: str,
        user_id: str,
    ) -> list[dict]:
        """Return MCP tool definitions enabled for this project.

        Each tool dict has: name, description, input_schema — ready to be
        passed directly to AIProvider.send_message(tools=...).
        """
        # 1. Get disabled MCP server IDs for this project
        disabled_rows = await self.db.fetch(
            "SELECT mcp_server_id FROM project_disabled_mcp_servers WHERE project_id = $1",
            project_id,
        )
        disabled_ids = {str(r["mcp_server_id"]) for r in disabled_rows}

        # 2. Get all active MCP servers owned by the user
        servers = await self.db.fetch(
            "SELECT id, name, tools_json FROM mcp_servers "
            "WHERE owner_id = $1 AND is_active = TRUE",
            user_id,
        )

        tools: list[dict] = []
        for srv in servers:
            if str(srv["id"]) in disabled_ids:
                continue

            raw = srv["tools_json"]
            if not raw:
                continue

            # asyncpg may return JSONB as string
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue

            if not isinstance(raw, list):
                continue

            server_name = srv["name"] or "mcp"
            for t in raw:
                tool_name = t.get("name", "")
                if not tool_name:
                    continue
                # Prefix with server name to avoid collisions across servers
                prefixed_name = f"{server_name}__{tool_name}" if len(servers) > 1 else tool_name
                tools.append({
                    "name": prefixed_name,
                    "description": t.get("description", ""),
                    "input_schema": t.get("input_schema", t.get("inputSchema", {})),
                    "_mcp_server_id": str(srv["id"]),
                    "_original_name": tool_name,
                })

        return tools

    async def resolve_skills(
        self,
        *,
        project_id: str,
        user_id: str,
    ) -> list[dict]:
        """Return enabled skill prompts for this project.

        Skills are not LLM tools — they're injected into the system prompt
        as additional instructions/capabilities.
        """
        disabled_rows = await self.db.fetch(
            "SELECT skill_id FROM project_disabled_skills WHERE project_id = $1",
            project_id,
        )
        disabled_ids = {str(r["skill_id"]) for r in disabled_rows}

        skills = await self.db.fetch(
            "SELECT id, name, description, prompt, category FROM skills "
            "WHERE owner_id = $1 AND is_active = TRUE",
            user_id,
        )

        return [
            {
                "name": s["name"],
                "description": s["description"] or "",
                "prompt": s["prompt"],
                "category": s["category"] or "general",
            }
            for s in skills
            if str(s["id"]) not in disabled_ids
        ]

    async def build_skills_context(
        self,
        *,
        project_id: str,
        user_id: str,
    ) -> str:
        """Build a system prompt section describing available skills."""
        skills = await self.resolve_skills(
            project_id=project_id, user_id=user_id
        )
        if not skills:
            return ""

        lines = ["\n\nAvailable skills/instructions:"]
        for s in skills:
            lines.append(f"- **{s['name']}**: {s['prompt']}")
        return "\n".join(lines)
