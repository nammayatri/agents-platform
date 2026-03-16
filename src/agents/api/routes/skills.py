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
            tools, new_transport, new_url = await _discover_tools_http_with_fallback(url, transport)
            # If transport/URL changed via fallback, update the server config
            if new_transport != transport or new_url != url:
                logger.info("MCP discovery: auto-detected transport change for %s: %s %s -> %s %s",
                            server_id, transport, url, new_transport, new_url)
                await db.execute(
                    "UPDATE mcp_servers SET transport = $2, url = $3, updated_at = NOW() WHERE id = $1",
                    server_id, new_transport, new_url,
                )
                transport = new_transport
                url = new_url
        else:
            raise ValueError(f"Unknown transport: {transport}")

        # Store discovered tools
        await db.execute(
            "UPDATE mcp_servers SET tools_json = $2::jsonb, updated_at = NOW() WHERE id = $1",
            server_id,
            json.dumps(tools),
        )

        result = {"status": "ok", "tools": tools}
        if transport != server.get("transport") or url != server.get("url"):
            result["transport_updated"] = transport
            result["url_updated"] = url
        return result

    except Exception as e:
        logger.warning("MCP tool discovery failed for %s: %s: %s", server_id, type(e).__name__, e)
        detail = str(e) or f"{type(e).__name__}"
        return {"status": "error", "detail": detail, "tools": []}


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


async def _discover_tools_http_with_fallback(
    url: str, transport: str,
) -> tuple[list[dict], str, str]:
    """Discover tools, falling back to alternate transport if primary fails.

    Returns (tools, effective_transport, effective_url).
    """
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(url)
    base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    # Primary attempt
    primary_error = None
    try:
        tools = await _discover_tools_http(url, transport)
        return tools, transport, url
    except Exception as e:
        primary_error = e
        logger.warning("MCP discovery primary (%s @ %s) failed: %s", transport, url, e)

    # Fallback: try the other transport at common paths
    fallback_attempts: list[tuple[str, str]] = []
    if transport == "sse":
        # SSE failed — try streamable-http at /mcp and base URL
        fallback_attempts.append(("streamable-http", base_url + "/mcp"))
        if parsed.path != "/mcp":
            fallback_attempts.append(("streamable-http", base_url))
    else:
        # streamable-http failed — try SSE at /sse
        fallback_attempts.append(("sse", base_url + "/sse"))
        if parsed.path != "/sse":
            fallback_attempts.append(("sse", base_url))

    for fb_transport, fb_url in fallback_attempts:
        try:
            logger.info("MCP discovery fallback: trying %s @ %s", fb_transport, fb_url)
            tools = await _discover_tools_http(fb_url, fb_transport)
            logger.info("MCP discovery fallback succeeded: %s @ %s", fb_transport, fb_url)
            return tools, fb_transport, fb_url
        except Exception as fb_err:
            logger.debug("MCP discovery fallback (%s @ %s) failed: %s", fb_transport, fb_url, fb_err)
            continue

    # All attempts failed — raise the primary error
    raise primary_error


async def _discover_tools_http(url: str, transport: str) -> list[dict]:
    """Discover tools from an SSE or streamable-http MCP server."""
    import httpx

    if transport == "sse":
        return await _discover_tools_sse(url)

    return await _discover_tools_streamable_http(url)


async def _discover_tools_streamable_http(url: str) -> list[dict]:
    """Discover tools from a streamable-http MCP server.

    Streamable-HTTP servers may return responses as SSE streams (text/event-stream)
    which stay open. We must use streaming reads to avoid hanging.
    """
    import httpx

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, connect=30.0), verify=False,
    ) as client:
        # 1. Initialize
        init_resp, init_headers = await _streamable_post(client, url, {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "agents-platform", "version": "0.1.0"},
            },
        }, headers)
        if "error" in init_resp:
            raise ValueError(f"MCP initialize error: {init_resp['error']}")

        # Capture session ID for subsequent requests (MCP spec requirement)
        session_id = init_headers.get("mcp-session-id")
        if session_id:
            headers = {**headers, "mcp-session-id": session_id}
            logger.info("MCP streamable-http: session_id=%s", session_id)

        # 2. Send initialized notification (fire-and-forget, no response expected)
        try:
            await _streamable_post(client, url, {
                "jsonrpc": "2.0", "method": "notifications/initialized",
            }, headers, expect_response=False)
        except Exception:
            pass  # Notifications may not return anything

        # 3. tools/list
        tools_resp, _ = await _streamable_post(client, url, {
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        }, headers)
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


async def _streamable_post(
    client, url: str, body: dict, headers: dict, *,
    expect_response: bool = True, timeout: float = 15,
) -> tuple[dict, dict]:
    """POST a JSON-RPC message and handle both JSON and SSE-stream responses.

    Returns (json_response, response_headers_dict).
    For SSE streams, reads line-by-line until a JSON-RPC response arrives,
    then closes the stream immediately to avoid hanging.
    """

    req = client.build_request("POST", url, content=json.dumps(body), headers=headers)
    resp = await client.send(req, stream=True)
    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    resp_headers = dict(resp.headers)

    try:
        if "text/event-stream" in content_type:
            if not expect_response:
                return {}, resp_headers
            deadline = asyncio.get_event_loop().time() + timeout
            async for raw_line in resp.aiter_lines():
                if asyncio.get_event_loop().time() > deadline:
                    raise TimeoutError("Timed out reading SSE response")
                line = raw_line.strip()
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if not data:
                        continue
                    try:
                        msg = json.loads(data)
                        if "id" in msg:
                            return msg, resp_headers
                    except json.JSONDecodeError:
                        continue
            if not expect_response:
                return {}, resp_headers
            raise ValueError("SSE stream ended without JSON-RPC response")
        else:
            raw = await resp.aread()
            if not expect_response:
                return {}, resp_headers
            return json.loads(raw), resp_headers
    finally:
        await resp.aclose()


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
                # Normalize CRLF → LF (SSE spec allows both)
                buf = buf.replace(b"\r\n", b"\n")
                while b"\n\n" in buf:
                    block, buf = buf.split(b"\n\n", 1)
                    etype = None
                    data_lines: list[str] = []
                    for line in block.decode(errors="replace").strip().split("\n"):
                        if line.startswith(":"):
                            continue  # SSE comment
                        if line.startswith("event:"):
                            etype = line[6:].strip()
                        elif line.startswith("data:"):
                            data_lines.append(line[5:].strip())
                    edata = "\n".join(data_lines) if data_lines else None
                    # SSE spec: default event type is "message"
                    if edata is not None:
                        await events.put((etype or "message", edata))
        except Exception as exc:
            await events.put(("error", str(exc)))

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=30.0), follow_redirects=True,
        verify=False,
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


@router.post("/mcp-servers/{server_id}/test-connection")
async def test_mcp_connection(server_id: str, user: CurrentUser, db: DB):
    """Test connectivity to an MCP server and return diagnostic info."""
    import httpx

    row = await db.fetchrow("SELECT * FROM mcp_servers WHERE id = $1", server_id)
    if not row:
        raise HTTPException(status_code=404, detail="MCP server not found")
    if str(row["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    server = dict(row)
    transport = server.get("transport", "stdio")
    url = server.get("url", "")

    if transport == "stdio":
        return {"status": "ok", "transport": "stdio", "message": "Stdio servers are tested during tool discovery"}

    if not url:
        return {"status": "error", "message": "No URL configured"}

    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(url)
    base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

    results = []

    # Probe common endpoints
    probe_paths = [
        parsed.path,  # configured path
        "/mcp",
        "/sse",
        "/health",
        "/ping",
    ]
    # Deduplicate while preserving order
    seen = set()
    unique_paths = []
    for p in probe_paths:
        if p and p not in seen:
            seen.add(p)
            unique_paths.append(p)

    async with httpx.AsyncClient(timeout=30, verify=False, follow_redirects=True) as client:
        for path in unique_paths:
            probe_url = base_url + path
            try:
                resp = await client.get(probe_url, headers={"Accept": "text/event-stream, application/json, */*"})
                content_type = resp.headers.get("content-type", "")
                results.append({
                    "path": path,
                    "url": probe_url,
                    "status": resp.status_code,
                    "content_type": content_type,
                    "body_preview": resp.text[:200] if resp.status_code < 400 else resp.text[:500],
                    "supports_sse": "text/event-stream" in content_type,
                })
            except Exception as e:
                results.append({
                    "path": path,
                    "url": probe_url,
                    "status": None,
                    "error": str(e),
                })

        # Try streamable-http POST to /mcp
        mcp_url = base_url + "/mcp"
        try:
            resp = await client.post(
                mcp_url,
                content=json.dumps({
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "agents-platform", "version": "0.1.0"},
                    },
                }),
                headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            )
            results.append({
                "path": "/mcp (POST initialize)",
                "url": mcp_url,
                "status": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "body_preview": resp.text[:500],
                "supports_streamable_http": resp.status_code == 200,
            })
        except Exception as e:
            results.append({
                "path": "/mcp (POST initialize)",
                "url": mcp_url,
                "error": str(e),
            })

    # Determine recommendation
    recommendation = None
    for r in results:
        if r.get("supports_streamable_http"):
            recommendation = {"transport": "streamable-http", "url": mcp_url}
            break
        if r.get("supports_sse") and r.get("status") == 200:
            recommendation = {"transport": "sse", "url": r["url"]}
            break

    return {
        "status": "ok",
        "current_transport": transport,
        "current_url": url,
        "probes": results,
        "recommendation": recommendation,
    }


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


@router.post("/git-providers/{gp_id}/test")
async def test_git_provider(gp_id: str, user: CurrentUser, db: DB):
    """Test git provider credentials by calling the provider's API."""
    import httpx

    row = await db.fetchrow("SELECT * FROM git_provider_configs WHERE id = $1", gp_id)
    if not row:
        raise HTTPException(status_code=404, detail="Git provider not found")
    if str(row["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403, detail="Access denied")

    token = decrypt(row["token_enc"]) if row.get("token_enc") else None
    if not token:
        return {"status": "error", "detail": "No token configured for this provider"}

    provider_type = row["provider_type"]
    api_base_url = row.get("api_base_url") or ""

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if provider_type == "github":
                base = api_base_url or "https://api.github.com"
                resp = await client.get(
                    f"{base.rstrip('/')}/user",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                )
                if resp.status_code == 200:
                    user_data = resp.json()
                    return {
                        "status": "ok",
                        "detail": f"Authenticated as {user_data.get('login', 'unknown')}",
                    }
                return {
                    "status": "error",
                    "detail": f"GitHub API returned {resp.status_code}: {resp.text[:200]}",
                }

            elif provider_type == "gitlab":
                base = api_base_url or "https://gitlab.com"
                resp = await client.get(
                    f"{base.rstrip('/')}/api/v4/user",
                    headers={"PRIVATE-TOKEN": token},
                )
                if resp.status_code == 200:
                    user_data = resp.json()
                    return {
                        "status": "ok",
                        "detail": f"Authenticated as {user_data.get('username', 'unknown')}",
                    }
                return {
                    "status": "error",
                    "detail": f"GitLab API returned {resp.status_code}: {resp.text[:200]}",
                }

            elif provider_type == "bitbucket":
                base = api_base_url or "https://api.bitbucket.org"
                resp = await client.get(
                    f"{base.rstrip('/')}/2.0/user",
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    user_data = resp.json()
                    return {
                        "status": "ok",
                        "detail": f"Authenticated as {user_data.get('display_name', 'unknown')}",
                    }
                return {
                    "status": "error",
                    "detail": f"Bitbucket API returned {resp.status_code}: {resp.text[:200]}",
                }

            else:
                return {"status": "error", "detail": f"Test not supported for provider type: {provider_type}"}

    except httpx.ConnectError as e:
        return {"status": "error", "detail": f"Connection failed: {e}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


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
