"""MCP tool executor: calls MCP server tools on behalf of agents.

When an LLM responds with tool_use, this module connects to the
appropriate MCP server and invokes the requested tool.
"""

import asyncio
import json
import logging
import os

import asyncpg

logger = logging.getLogger(__name__)


class McpToolExecutor:
    def __init__(self, db: asyncpg.Pool):
        self.db = db

    async def execute_tool(
        self,
        tool_name: str,
        tool_input: dict,
        mcp_tools: list[dict],
    ) -> str:
        """Execute a tool call by finding its MCP server and invoking it.

        Args:
            tool_name: The tool name from the LLM response
            tool_input: The tool arguments from the LLM response
            mcp_tools: The resolved tools list (with _mcp_server_id or _builtin metadata)

        Returns:
            The tool result as a string.
        """
        logger.info("execute_tool: %s args=%s", tool_name, {k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v) for k, v in tool_input.items()})

        # Find which MCP server owns this tool
        tool_meta = None
        for t in mcp_tools:
            if t["name"] == tool_name:
                tool_meta = t
                break

        if not tool_meta:
            logger.warning("execute_tool: tool '%s' not found in %d registered tools", tool_name, len(mcp_tools))
            return json.dumps({"error": f"Tool '{tool_name}' not found"})

        # Handle built-in workspace tools (no MCP server needed)
        if tool_meta.get("_builtin"):
            return await self._execute_builtin(tool_meta, tool_input)

        server_id = tool_meta.get("_mcp_server_id")
        original_name = tool_meta.get("_original_name", tool_name)

        if not server_id:
            return json.dumps({"error": f"No MCP server for tool '{tool_name}'"})

        # Fetch server config
        server = await self.db.fetchrow(
            "SELECT * FROM mcp_servers WHERE id = $1", server_id
        )
        if not server:
            return json.dumps({"error": f"MCP server {server_id} not found"})

        server = dict(server)
        transport = server.get("transport", "stdio")

        try:
            if transport == "stdio":
                return await self._call_stdio(server, original_name, tool_input)
            elif transport in ("sse", "streamable-http"):
                return await self._call_http(server, original_name, tool_input, transport)
            else:
                return json.dumps({"error": f"Unknown transport: {transport}"})
        except Exception as e:
            logger.warning("MCP tool call failed: %s / %s: %s", tool_name, server_id, e)
            return json.dumps({"error": str(e)})

    async def _execute_builtin(self, tool_meta: dict, tool_input: dict) -> str:
        """Execute a built-in workspace tool directly (no MCP server)."""
        workspace_path = tool_meta.get("_workspace_path", "")
        repo_dir = os.path.join(workspace_path, "repo") if workspace_path else ""
        tool_name = tool_meta["name"]

        logger.info("builtin[%s]: workspace=%s repo_dir=%s", tool_name, workspace_path, repo_dir)

        try:
            if tool_name == "read_file":
                path = tool_input.get("path", "")
                full_path = os.path.join(repo_dir, path)
                # Security: ensure path stays within repo
                real_path = os.path.realpath(full_path)
                real_repo = os.path.realpath(repo_dir)
                if not real_path.startswith(real_repo):
                    logger.warning("builtin[read_file]: path traversal blocked: %s", path)
                    return json.dumps({"error": "Path traversal not allowed"})
                if not os.path.isfile(real_path):
                    logger.info("builtin[read_file]: file not found: %s (resolved: %s)", path, real_path)
                    return json.dumps({"error": f"File not found: {path}"})
                with open(real_path, "r", errors="replace") as f:
                    content = f.read()
                # Truncate very large files
                if len(content) > 100_000:
                    logger.info("builtin[read_file]: %s truncated from %d to 100000 chars", path, len(content))
                    content = content[:100_000] + "\n... (truncated)"
                logger.info("builtin[read_file]: %s → %d chars", path, len(content))
                return content

            elif tool_name == "write_file":
                path = tool_input.get("path", "")
                content = tool_input.get("content", "")
                full_path = os.path.join(repo_dir, path)
                real_path = os.path.realpath(full_path)
                real_repo = os.path.realpath(repo_dir)
                if not real_path.startswith(real_repo):
                    logger.warning("builtin[write_file]: path traversal blocked: %s", path)
                    return json.dumps({"error": "Path traversal not allowed"})
                os.makedirs(os.path.dirname(real_path), exist_ok=True)
                with open(real_path, "w") as f:
                    f.write(content)
                logger.info("builtin[write_file]: %s → %d bytes written (resolved: %s)", path, len(content), real_path)
                return json.dumps({"status": "ok", "path": path, "bytes_written": len(content)})

            elif tool_name == "list_directory":
                path = tool_input.get("path", "")
                full_path = os.path.join(repo_dir, path) if path else repo_dir
                real_path = os.path.realpath(full_path)
                real_repo = os.path.realpath(repo_dir)
                if not real_path.startswith(real_repo):
                    logger.warning("builtin[list_directory]: path traversal blocked: %s", path)
                    return json.dumps({"error": "Path traversal not allowed"})
                if not os.path.isdir(real_path):
                    logger.info("builtin[list_directory]: directory not found: %s (resolved: %s)", path, real_path)
                    return json.dumps({"error": f"Directory not found: {path}"})
                entries = sorted(os.listdir(real_path))
                result = []
                for e in entries[:500]:  # limit entries
                    full_e = os.path.join(real_path, e)
                    result.append({
                        "name": e,
                        "type": "directory" if os.path.isdir(full_e) else "file",
                    })
                logger.info("builtin[list_directory]: %s → %d entries", path or "/", len(result))
                return json.dumps(result)

            elif tool_name == "search_files":
                pattern = tool_input.get("pattern", "")
                if not pattern:
                    return json.dumps({"error": "No search pattern provided"})
                search_path = tool_input.get("path", "")
                file_glob = tool_input.get("file_glob", "*")
                full_search = os.path.join(repo_dir, search_path) if search_path else repo_dir
                real_search = os.path.realpath(full_search)
                real_repo = os.path.realpath(repo_dir)
                if not real_search.startswith(real_repo):
                    logger.warning("builtin[search_files]: path traversal blocked: %s", search_path)
                    return json.dumps({"error": "Path traversal not allowed"})
                if not os.path.isdir(real_search):
                    logger.info("builtin[search_files]: directory not found: %s", search_path)
                    return json.dumps({"error": f"Directory not found: {search_path}"})

                logger.info("builtin[search_files]: pattern=%s path=%s glob=%s", pattern, search_path or "/", file_glob)
                cmd = ["grep", "-rn", "--include", file_glob, pattern, "."]
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=real_search,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
                    output = stdout.decode(errors="replace")
                    if len(output) > 50_000:
                        logger.info("builtin[search_files]: output truncated from %d to 50000 chars", len(output))
                        output = output[:50_000] + "\n... (truncated)"
                    match_count = output.count("\n") if output else 0
                    logger.info("builtin[search_files]: %d matching lines, %d chars", match_count, len(output))
                    return output if output else "No matches found."
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.warning("builtin[search_files]: timed out after 30s (pattern=%s)", pattern)
                    return json.dumps({"error": "Search timed out after 30s"})

            elif tool_name == "run_command":
                command = tool_input.get("command", "")
                if not command:
                    return json.dumps({"error": "No command provided"})
                logger.info("builtin[run_command]: cmd=%s cwd=%s", command[:200], repo_dir)
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=repo_dir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                    output = stdout.decode(errors="replace")
                    if len(output) > 50_000:
                        logger.info("builtin[run_command]: output truncated from %d to 50000 chars", len(output))
                        output = output[:50_000] + "\n... (truncated)"
                    logger.info("builtin[run_command]: exit_code=%d output_len=%d cmd=%s",
                                proc.returncode, len(output), command[:100])
                    return json.dumps({
                        "exit_code": proc.returncode,
                        "output": output,
                    })
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.warning("builtin[run_command]: timed out after 120s cmd=%s", command[:200])
                    return json.dumps({"error": "Command timed out after 120s"})

            else:
                logger.warning("builtin: unknown tool: %s", tool_name)
                return json.dumps({"error": f"Unknown built-in tool: {tool_name}"})

        except Exception as e:
            logger.error("builtin[%s]: failed with exception: %s", tool_name, e, exc_info=True)
            return json.dumps({"error": str(e)})

    async def _call_stdio(
        self, server: dict, tool_name: str, tool_input: dict
    ) -> str:
        """Call a tool on a stdio MCP server."""
        env = {
            **os.environ,
            **{k: str(v) for k, v in (server.get("env_json") or {}).items()},
        }
        # Parse env_json if string
        env_json = server.get("env_json") or {}
        if isinstance(env_json, str):
            env_json = json.loads(env_json)
        env = {**os.environ, **{k: str(v) for k, v in env_json.items()}}

        proc = await asyncio.create_subprocess_exec(
            server["command"],
            *(server.get("args") or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        try:
            # Initialize
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

            init_resp = await asyncio.wait_for(
                self._read_jsonrpc(proc.stdout), timeout=30
            )
            if "error" in init_resp:
                return json.dumps({"error": f"MCP init error: {init_resp['error']}"})

            # Initialized notification
            notif = json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            })
            proc.stdin.write(notif.encode() + b"\n")
            await proc.stdin.drain()

            # Call the tool
            call_msg = json.dumps({
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": tool_name,
                    "arguments": tool_input,
                },
            })
            proc.stdin.write(call_msg.encode() + b"\n")
            await proc.stdin.drain()

            result = await asyncio.wait_for(
                self._read_jsonrpc(proc.stdout), timeout=60
            )

            if "error" in result:
                return json.dumps({"error": result["error"]})

            # Extract content from result
            content = result.get("result", {}).get("content", [])
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
                elif isinstance(block, str):
                    texts.append(block)
            return "\n".join(texts) if texts else json.dumps(result.get("result", {}))

        finally:
            proc.stdin.close()
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()

    async def _call_http(
        self, server: dict, tool_name: str, tool_input: dict, transport: str
    ) -> str:
        """Call a tool on an HTTP/SSE MCP server."""
        import httpx

        url = server.get("url")
        if not url:
            return json.dumps({"error": "No URL configured for HTTP MCP server"})

        if transport == "sse":
            return await self._call_sse(url, tool_name, tool_input)

        # streamable-http: direct POST
        async with httpx.AsyncClient(timeout=60) as client:
            # Initialize
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agents-platform", "version": "0.1.0"},
                },
            })
            resp.raise_for_status()

            # Call tool
            resp = await client.post(url, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool_name, "arguments": tool_input},
            })
            resp.raise_for_status()
            result = resp.json()

            if "error" in result:
                return json.dumps({"error": result["error"]})

            content = result.get("result", {}).get("content", [])
            texts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            return "\n".join(texts) if texts else json.dumps(result.get("result", {}))

    async def _call_sse(self, url: str, tool_name: str, tool_input: dict) -> str:
        """Call a tool via SSE transport."""
        import httpx
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(url)
        base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))

        events: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

        async def _sse_reader(resp: httpx.Response) -> None:
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
            timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=True
        ) as client:
            req = client.build_request("GET", url, headers={"Accept": "text/event-stream"})
            resp = await client.send(req, stream=True)
            resp.raise_for_status()
            reader = asyncio.create_task(_sse_reader(resp))

            try:
                # Wait for endpoint
                etype, edata = await asyncio.wait_for(events.get(), timeout=15)
                if etype != "endpoint":
                    return json.dumps({"error": f"Expected endpoint event, got {etype}"})
                post_url = edata if edata.startswith("http") else base_url + edata

                # Initialize
                await client.post(post_url, json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "agents-platform", "version": "0.1.0"},
                    },
                })
                await self._wait_sse_response(events)

                # Initialized notification
                await client.post(post_url, json={
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                })

                # Call tool
                await client.post(post_url, json={
                    "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": tool_name, "arguments": tool_input},
                })
                result = await self._wait_sse_response(events)

                if "error" in result:
                    return json.dumps({"error": result["error"]})

                content = result.get("result", {}).get("content", [])
                texts = []
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        texts.append(block.get("text", ""))
                return "\n".join(texts) if texts else json.dumps(result.get("result", {}))

            finally:
                reader.cancel()
                await resp.aclose()

    @staticmethod
    async def _read_jsonrpc(stdout: asyncio.StreamReader) -> dict:
        """Read JSON-RPC response from stdout."""
        while True:
            line = await stdout.readline()
            if not line:
                raise ValueError("MCP server closed stdout")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                if "id" in msg:
                    return msg
            except json.JSONDecodeError:
                continue

    @staticmethod
    async def _wait_sse_response(
        events: asyncio.Queue, *, timeout: float = 30,
    ) -> dict:
        """Wait for a JSON-RPC response from SSE events."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for MCP response")
            etype, edata = await asyncio.wait_for(events.get(), timeout=remaining)
            if etype == "error":
                raise ValueError(f"SSE error: {edata}")
            if etype == "message":
                try:
                    msg = json.loads(edata)
                    if "id" in msg:
                        return msg
                except json.JSONDecodeError:
                    continue
