import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agents.api.deps import DB, CurrentUser, check_project_access, check_project_owner
from agents.infra.crypto import decrypt, encrypt

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────


class SkillInput(BaseModel):
    name: str
    description: str | None = None
    prompt: str
    category: str = "general"


class SkillUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    prompt: str | None = None
    category: str | None = None
    is_active: bool | None = None


class McpServerInput(BaseModel):
    name: str
    description: str | None = None
    command: str
    args: list[str] = []
    env_json: dict = {}
    transport: str = "stdio"
    url: str | None = None


class McpServerUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    command: str | None = None
    args: list[str] | None = None
    env_json: dict | None = None
    transport: str | None = None
    url: str | None = None
    is_active: bool | None = None


# ── Skills CRUD ──────────────────────────────────────────────────────


@router.get("/skills")
async def list_skills(user: CurrentUser, db: DB):
    rows = await db.fetch(
        "SELECT * FROM skills WHERE owner_id = $1 ORDER BY created_at DESC",
        user["id"],
    )
    return [dict(r) for r in rows]


@router.post("/skills", status_code=status.HTTP_201_CREATED)
async def create_skill(body: SkillInput, user: CurrentUser, db: DB):
    row = await db.fetchrow(
        """
        INSERT INTO skills (owner_id, name, description, prompt, category)
        VALUES ($1, $2, $3, $4, $5) RETURNING *
        """,
        user["id"],
        body.name,
        body.description,
        body.prompt,
        body.category,
    )
    return dict(row)


@router.put("/skills/{skill_id}")
async def update_skill(skill_id: str, body: SkillUpdate, user: CurrentUser, db: DB):
    existing = await db.fetchrow("SELECT * FROM skills WHERE id = $1", skill_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    if str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return dict(existing)

    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items()):
        set_parts.append(f"{k} = ${i+2}")
        values.append(v)

    row = await db.fetchrow(
        f"UPDATE skills SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = $1 RETURNING *",
        skill_id,
        *values,
    )
    return dict(row)


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(skill_id: str, user: CurrentUser, db: DB):
    existing = await db.fetchrow(
        "SELECT owner_id FROM skills WHERE id = $1", skill_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Skill not found")
    if str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")
    await db.execute("DELETE FROM skills WHERE id = $1", skill_id)


# ── MCP Servers CRUD ────────────────────────────────────────────────


@router.get("/mcp-servers")
async def list_mcp_servers(user: CurrentUser, db: DB):
    rows = await db.fetch(
        "SELECT * FROM mcp_servers WHERE owner_id = $1 ORDER BY created_at DESC",
        user["id"],
    )
    results = []
    for r in rows:
        d = dict(r)
        # Ensure tools_json is a parsed list, not a JSON string
        if isinstance(d.get("tools_json"), str):
            try:
                d["tools_json"] = json.loads(d["tools_json"])
            except (json.JSONDecodeError, TypeError):
                d["tools_json"] = None
        results.append(d)
    return results


@router.post("/mcp-servers", status_code=status.HTTP_201_CREATED)
async def create_mcp_server(body: McpServerInput, user: CurrentUser, db: DB):
    row = await db.fetchrow(
        """
        INSERT INTO mcp_servers (owner_id, name, description, command, args, env_json, transport, url)
        VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8) RETURNING *
        """,
        user["id"],
        body.name,
        body.description,
        body.command,
        body.args,
        json.dumps(body.env_json),
        body.transport,
        body.url,
    )
    return dict(row)


@router.put("/mcp-servers/{server_id}")
async def update_mcp_server(server_id: str, body: McpServerUpdate, user: CurrentUser, db: DB):
    existing = await db.fetchrow("SELECT * FROM mcp_servers WHERE id = $1", server_id)
    if not existing:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return dict(existing)

    # Handle env_json serialization
    if "env_json" in updates:
        updates["env_json"] = json.dumps(updates["env_json"])

    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items()):
        if k == "env_json":
            set_parts.append(f"{k} = ${i+2}::jsonb")
        else:
            set_parts.append(f"{k} = ${i+2}")
        values.append(v)

    row = await db.fetchrow(
        f"UPDATE mcp_servers SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = $1 RETURNING *",
        server_id,
        *values,
    )
    return dict(row)


@router.delete("/mcp-servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(server_id: str, user: CurrentUser, db: DB):
    existing = await db.fetchrow(
        "SELECT owner_id FROM mcp_servers WHERE id = $1", server_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")
    await db.execute("DELETE FROM mcp_servers WHERE id = $1", server_id)


@router.post("/mcp-servers/{server_id}/discover-tools")
async def discover_mcp_tools(server_id: str, user: CurrentUser, db: DB):
    """Connect to an MCP server, discover available tools, and store them."""
    row = await db.fetchrow("SELECT * FROM mcp_servers WHERE id = $1", server_id)
    if not row:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if str(row["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    server = dict(row)
    transport = server.get("transport", "stdio")

    try:
        if transport == "stdio":
            tools = await _discover_tools_stdio(
                command=server["command"],
                args=server.get("args") or [],
                env_json=server.get("env_json") or {},
            )
        elif transport in ("sse", "streamable-http"):
            url = server.get("url")
            if not url:
                raise ValueError("URL is required for SSE/HTTP transport")
            tools = await _discover_tools_http(url, transport)
        else:
            raise ValueError(f"Unknown transport: {transport}")

        # Store discovered tools
        await db.execute(
            "UPDATE mcp_servers SET tools_json = $2::jsonb, updated_at = NOW() WHERE id = $1",
            server_id,
            json.dumps(tools),
        )

        return {"status": "ok", "tools": tools}

    except Exception as e:
        logger.warning("MCP tool discovery failed for %s: %s", server_id, e)
        return {"status": "error", "detail": str(e), "tools": []}


async def _discover_tools_stdio(
    command: str,
    args: list[str],
    env_json: dict,
) -> list[dict]:
    """Spawn an MCP stdio server, send initialize + tools/list, return tools."""
    env = {**__import__("os").environ, **{k: str(v) for k, v in env_json.items()}}

    proc = await asyncio.create_subprocess_exec(
        command, *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    try:
        # MCP uses JSON-RPC 2.0 over stdio, newline-delimited
        init_msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agents-platform", "version": "0.1.0"},
            },
        })
        proc.stdin.write(init_msg.encode() + b"\n")
        await proc.stdin.drain()

        # Read initialize response
        init_resp = await asyncio.wait_for(_read_jsonrpc_line(proc.stdout), timeout=30)
        if "error" in init_resp:
            raise ValueError(f"MCP initialize error: {init_resp['error']}")

        # Send initialized notification
        notif = json.dumps({
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        proc.stdin.write(notif.encode() + b"\n")
        await proc.stdin.drain()

        # Send tools/list
        tools_msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        })
        proc.stdin.write(tools_msg.encode() + b"\n")
        await proc.stdin.drain()

        # Read tools/list response
        tools_resp = await asyncio.wait_for(_read_jsonrpc_line(proc.stdout), timeout=30)
        if "error" in tools_resp:
            raise ValueError(f"MCP tools/list error: {tools_resp['error']}")

        tools = tools_resp.get("result", {}).get("tools", [])
        return [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema", {}),
            }
            for t in tools
        ]

    finally:
        proc.stdin.close()
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def _read_jsonrpc_line(stdout: asyncio.StreamReader) -> dict:
    """Read lines from stdout until we get a valid JSON-RPC response (with an id)."""
    while True:
        line = await stdout.readline()
        if not line:
            raise ValueError("MCP server closed stdout unexpectedly")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
            # Skip notifications (no id), only return responses
            if "id" in msg:
                return msg
        except json.JSONDecodeError:
            continue


async def _discover_tools_http(url: str, transport: str) -> list[dict]:
    """Discover tools from an SSE or streamable-http MCP server."""
    import httpx

    if transport == "sse":
        return await _discover_tools_sse(url)

    # streamable-http: POST JSON-RPC directly
    async with httpx.AsyncClient(timeout=30) as client:
        init_body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "agents-platform", "version": "0.1.0"},
            },
        }
        resp = await client.post(url, json=init_body)
        resp.raise_for_status()
        init_resp = resp.json()
        if "error" in init_resp:
            raise ValueError(f"MCP initialize error: {init_resp['error']}")

        # Send tools/list
        tools_body = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/list",
            "params": {},
        }
        resp = await client.post(url, json=tools_body)
        resp.raise_for_status()
        tools_resp = resp.json()
        if "error" in tools_resp:
            raise ValueError(f"MCP tools/list error: {tools_resp['error']}")

        tools = tools_resp.get("result", {}).get("tools", [])
        return [
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
                "input_schema": t.get("inputSchema", {}),
            }
            for t in tools
        ]


async def _discover_tools_sse(url: str) -> list[dict]:
    """Discover tools from an SSE MCP server.

    SSE MCP protocol:
    1. Connect to the SSE endpoint (GET) to receive server-sent events
    2. The server sends an 'endpoint' event with a POST URL for sending messages
    3. POST JSON-RPC messages to that endpoint, receive responses via SSE stream
    """
    import httpx

    # Derive base URL (e.g. https://host.com from https://host.com/sse)
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    events: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def _sse_reader(resp: httpx.Response) -> None:
        """Background task: continuously read SSE events into the queue."""
        buf = b""
        try:
            async for chunk in resp.aiter_bytes():
                buf += chunk
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    etype = edata = None
                    for line in block.decode(errors="replace").strip().split("\n"):
                        if line.startswith("event:"):
                            etype = line[6:].strip()
                        elif line.startswith("data:"):
                            edata = line[5:].strip()
                    if etype and edata:
                        await events.put((etype, edata))
        except Exception as exc:
            await events.put(("error", str(exc)))

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True,
    ) as client:
        # Open the SSE stream
        req = client.build_request("GET", url, headers={"Accept": "text/event-stream"})
        resp = await client.send(req, stream=True)
        resp.raise_for_status()

        reader = asyncio.create_task(_sse_reader(resp))

        try:
            # 1. Wait for the endpoint event
            etype, edata = await asyncio.wait_for(events.get(), timeout=15)
            if etype == "error":
                raise ValueError(f"SSE stream error: {edata}")
            if etype != "endpoint":
                raise ValueError(f"Expected 'endpoint' event, got '{etype}'")

            post_url = edata if edata.startswith("http") else base_url + edata

            # 2. Initialize
            await client.post(post_url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agents-platform", "version": "0.1.0"},
                },
            })

            # Wait for init response on SSE
            init_resp = await _wait_for_jsonrpc_response(events, timeout=15)
            if "error" in init_resp:
                raise ValueError(f"MCP initialize error: {init_resp['error']}")

            # 3. Send initialized notification
            await client.post(post_url, json={
                "jsonrpc": "2.0", "method": "notifications/initialized",
            })

            # 4. Request tools/list
            await client.post(post_url, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
            })

            tools_resp = await _wait_for_jsonrpc_response(events, timeout=15)
            if "error" in tools_resp:
                raise ValueError(f"MCP tools/list error: {tools_resp['error']}")

            raw_tools = tools_resp.get("result", {}).get("tools", [])
            return [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "input_schema": t.get("inputSchema", {}),
                }
                for t in raw_tools
            ]

        finally:
            reader.cancel()
            await resp.aclose()


async def _wait_for_jsonrpc_response(
    events: asyncio.Queue, *, timeout: float = 15,
) -> dict:
    """Consume SSE events from the queue until a JSON-RPC response (has 'id') arrives."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            raise TimeoutError("Timed out waiting for MCP response")
        etype, edata = await asyncio.wait_for(events.get(), timeout=remaining)
        if etype == "error":
            raise ValueError(f"SSE stream error: {edata}")
        if etype == "message":
            try:
                msg = json.loads(edata)
                if "id" in msg:
                    return msg
            except json.JSONDecodeError:
                continue


# ── Per-project enablement ──────────────────────────────────────────


class ProjectToggleInput(BaseModel):
    disabled_skill_ids: list[str] = []
    disabled_mcp_server_ids: list[str] = []
    disabled_provider_ids: list[str] = []


@router.get("/projects/{project_id}/enabled")
async def get_project_enablement(project_id: str, user: CurrentUser, db: DB):
    """Get which skills, MCP servers, and providers are disabled for a project."""
    await check_project_access(db, project_id, user)

    disabled_skills = await db.fetch(
        "SELECT skill_id FROM project_disabled_skills WHERE project_id = $1",
        project_id,
    )
    disabled_mcp = await db.fetch(
        "SELECT mcp_server_id FROM project_disabled_mcp_servers WHERE project_id = $1",
        project_id,
    )
    disabled_providers = await db.fetch(
        "SELECT provider_id FROM project_disabled_providers WHERE project_id = $1",
        project_id,
    )

    return {
        "disabled_skill_ids": [str(r["skill_id"]) for r in disabled_skills],
        "disabled_mcp_server_ids": [str(r["mcp_server_id"]) for r in disabled_mcp],
        "disabled_provider_ids": [str(r["provider_id"]) for r in disabled_providers],
    }


@router.put("/projects/{project_id}/enabled")
async def update_project_enablement(
    project_id: str, body: ProjectToggleInput, user: CurrentUser, db: DB
):
    """Set which skills, MCP servers, and providers are disabled for a project."""
    await check_project_owner(db, project_id, user)

    # Replace disabled skills
    await db.execute(
        "DELETE FROM project_disabled_skills WHERE project_id = $1", project_id
    )
    for sid in body.disabled_skill_ids:
        await db.execute(
            "INSERT INTO project_disabled_skills (project_id, skill_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            project_id, sid,
        )

    # Replace disabled MCP servers
    await db.execute(
        "DELETE FROM project_disabled_mcp_servers WHERE project_id = $1", project_id
    )
    for mid in body.disabled_mcp_server_ids:
        await db.execute(
            "INSERT INTO project_disabled_mcp_servers (project_id, mcp_server_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            project_id, mid,
        )

    # Replace disabled providers
    await db.execute(
        "DELETE FROM project_disabled_providers WHERE project_id = $1", project_id
    )
    for pid in body.disabled_provider_ids:
        await db.execute(
            "INSERT INTO project_disabled_providers (project_id, provider_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            project_id, pid,
        )

    return {"status": "updated"}


# ── Git Provider Configs CRUD ───────────────────────────────────────


class GitProviderInput(BaseModel):
    provider_type: str  # 'github' | 'gitlab' | 'bitbucket' | 'custom'
    display_name: str
    api_base_url: str | None = None
    token: str | None = None


class GitProviderUpdate(BaseModel):
    provider_type: str | None = None
    display_name: str | None = None
    api_base_url: str | None = None
    token: str | None = None
    is_active: bool | None = None


def _sanitize_git_provider(row: dict) -> dict:
    """Remove encrypted token, add has_token indicator."""
    result = dict(row)
    has_token = bool(result.pop("token_enc", None))
    result["has_token"] = has_token
    return result


@router.get("/git-providers")
async def list_git_providers(user: CurrentUser, db: DB):
    rows = await db.fetch(
        "SELECT * FROM git_provider_configs WHERE owner_id = $1 ORDER BY created_at DESC",
        user["id"],
    )
    return [_sanitize_git_provider(dict(r)) for r in rows]


@router.post("/git-providers", status_code=status.HTTP_201_CREATED)
async def create_git_provider(body: GitProviderInput, user: CurrentUser, db: DB):
    token_enc = encrypt(body.token) if body.token else None
    row = await db.fetchrow(
        """
        INSERT INTO git_provider_configs (owner_id, provider_type, display_name, api_base_url, token_enc)
        VALUES ($1, $2, $3, $4, $5) RETURNING *
        """,
        user["id"],
        body.provider_type,
        body.display_name,
        body.api_base_url,
        token_enc,
    )
    return _sanitize_git_provider(dict(row))


@router.put("/git-providers/{gp_id}")
async def update_git_provider(gp_id: str, body: GitProviderUpdate, user: CurrentUser, db: DB):
    existing = await db.fetchrow("SELECT * FROM git_provider_configs WHERE id = $1", gp_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Git provider not found")
    if str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if not updates:
        return _sanitize_git_provider(dict(existing))

    # Handle token: encrypt before storage, map to token_enc column
    if "token" in updates:
        raw_token = updates.pop("token")
        if raw_token:
            updates["token_enc"] = encrypt(raw_token)

    if not updates:
        return _sanitize_git_provider(dict(existing))

    set_parts = []
    values = []
    for i, (k, v) in enumerate(updates.items()):
        set_parts.append(f"{k} = ${i+2}")
        values.append(v)

    row = await db.fetchrow(
        f"UPDATE git_provider_configs SET {', '.join(set_parts)}, updated_at = NOW() WHERE id = $1 RETURNING *",
        gp_id,
        *values,
    )
    return _sanitize_git_provider(dict(row))


@router.delete("/git-providers/{gp_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_git_provider(gp_id: str, user: CurrentUser, db: DB):
    existing = await db.fetchrow(
        "SELECT owner_id FROM git_provider_configs WHERE id = $1", gp_id
    )
    if not existing:
        raise HTTPException(status_code=404, detail="Git provider not found")
    if str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")
    await db.execute("DELETE FROM git_provider_configs WHERE id = $1", gp_id)
