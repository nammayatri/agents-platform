"""MCP tool executor: calls MCP server tools on behalf of agents.

When an LLM responds with tool_use, this module connects to the
appropriate MCP server and invokes the requested tool.
"""

import asyncio
import json
import logging
import os
import threading
from collections import OrderedDict

import asyncpg

logger = logging.getLogger(__name__)


# ── LRU File Cache ─────────────────────────────────────────────────
# Process-wide cache shared across McpToolExecutor instances.
# Keyed by absolute real_path for uniqueness.
# write_file updates the cache, read_file serves from cache when fresh.

MAX_CACHE_BYTES = 100 * 1024 * 1024  # 100 MB total memory cap
MAX_SINGLE_FILE = 5 * 1024 * 1024     # Don't cache files larger than 5 MB


class _CacheEntry:
    __slots__ = ("content", "mtime", "size")

    def __init__(self, content: str, mtime: float, size: int):
        self.content = content
        self.mtime = mtime     # filesystem mtime at time of caching
        self.size = size       # len(content) as byte count (approx)


class FileCache:
    """Thread-safe LRU file cache with mtime validation and memory cap."""

    def __init__(self, max_bytes: int = MAX_CACHE_BYTES):
        self._lock = threading.Lock()
        self._store: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._total_bytes: int = 0
        self._max_bytes = max_bytes
        self._hits = 0
        self._misses = 0

    def get(self, real_path: str) -> str | None:
        """Return cached content if fresh, else None.

        Freshness is validated by comparing cached mtime against the
        current filesystem mtime.  This ensures external writes (git
        operations, other processes) are never served stale.
        """
        with self._lock:
            entry = self._store.get(real_path)
            if entry is None:
                self._misses += 1
                return None

            # Validate mtime against disk
            try:
                disk_mtime = os.path.getmtime(real_path)
            except OSError:
                # File was deleted — evict and miss
                self._evict_entry(real_path)
                self._misses += 1
                return None

            if disk_mtime != entry.mtime:
                # File changed on disk — evict stale entry
                self._evict_entry(real_path)
                self._misses += 1
                return None

            # Cache hit — move to end (most recently used)
            self._store.move_to_end(real_path)
            self._hits += 1
            return entry.content

    def put(self, real_path: str, content: str, mtime: float | None = None) -> None:
        """Insert or update a cached file.  Evicts LRU entries to stay under cap."""
        byte_size = len(content.encode("utf-8", errors="replace"))
        if byte_size > MAX_SINGLE_FILE:
            # Don't cache very large files — they'd dominate the cache
            return

        if mtime is None:
            try:
                mtime = os.path.getmtime(real_path)
            except OSError:
                return

        with self._lock:
            # Remove old entry if exists
            if real_path in self._store:
                self._evict_entry(real_path)

            # Evict LRU entries until we have room
            while self._total_bytes + byte_size > self._max_bytes and self._store:
                _, evicted = self._store.popitem(last=False)  # pop oldest
                self._total_bytes -= evicted.size

            self._store[real_path] = _CacheEntry(content, mtime, byte_size)
            self._total_bytes += byte_size

    def invalidate(self, real_path: str) -> None:
        """Remove a specific path from the cache."""
        with self._lock:
            self._evict_entry(real_path)

    def _evict_entry(self, real_path: str) -> None:
        """Remove entry and update size tracking. Caller must hold lock."""
        entry = self._store.pop(real_path, None)
        if entry:
            self._total_bytes -= entry.size

    @property
    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._store),
                "total_bytes": self._total_bytes,
                "hits": self._hits,
                "misses": self._misses,
            }


# Singleton cache instance
_file_cache = FileCache()


class McpToolExecutor:
    def __init__(self, db: asyncpg.Pool):
        self.db = db
        self.file_cache = _file_cache

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
            logger.warning("MCP tool call failed (%s @ %s): %s / %s: %s",
                           transport, server.get("url"), tool_name, server_id, e)
            # Fallback: try alternate transport
            if transport in ("sse", "streamable-http"):
                try:
                    fb_transport, fb_url = self._fallback_transport(transport, server.get("url", ""))
                    if fb_transport:
                        logger.info("MCP tool call fallback: trying %s @ %s", fb_transport, fb_url)
                        fb_server = {**server, "url": fb_url}
                        result = await self._call_http(fb_server, original_name, tool_input, fb_transport)
                        # Fallback worked — update DB for future calls
                        await self.db.execute(
                            "UPDATE mcp_servers SET transport = $2, url = $3, updated_at = NOW() WHERE id = $1",
                            server_id, fb_transport, fb_url,
                        )
                        logger.info("MCP auto-updated server %s: %s -> %s @ %s", server_id, transport, fb_transport, fb_url)
                        return result
                except Exception as fb_err:
                    logger.debug("MCP fallback also failed: %s", fb_err)
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

                # Try cache first (validates mtime freshness automatically)
                content = self.file_cache.get(real_path)
                if content is not None:
                    logger.info("builtin[read_file]: %s → %d chars (cache hit)", path, len(content))
                    return content

                # Cache miss — read from disk
                with open(real_path, "r", errors="replace") as f:
                    content = f.read()
                # Truncate very large files
                if len(content) > 100_000:
                    logger.info("builtin[read_file]: %s truncated from %d to 100000 chars", path, len(content))
                    content = content[:100_000] + "\n... (truncated)"
                else:
                    # Cache the full (non-truncated) content
                    self.file_cache.put(real_path, content)
                logger.info("builtin[read_file]: %s → %d chars (disk)", path, len(content))
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
                # Update cache with the written content (mtime from disk after write)
                self.file_cache.put(real_path, content)
                logger.info("builtin[write_file]: %s → %d bytes written (resolved: %s)", path, len(content), real_path)
                return json.dumps({"status": "ok", "path": path, "bytes_written": len(content)})

            elif tool_name == "edit_file":
                path = tool_input.get("path", "")
                old_text = tool_input.get("old_text", "")
                new_text = tool_input.get("new_text", "")
                if not old_text:
                    return json.dumps({"error": "old_text is required"})
                full_path = os.path.join(repo_dir, path)
                real_path = os.path.realpath(full_path)
                real_repo = os.path.realpath(repo_dir)
                if not real_path.startswith(real_repo):
                    logger.warning("builtin[edit_file]: path traversal blocked: %s", path)
                    return json.dumps({"error": "Path traversal not allowed"})
                if not os.path.isfile(real_path):
                    logger.info("builtin[edit_file]: file not found: %s", path)
                    return json.dumps({"error": f"File not found: {path}"})

                with open(real_path, "r", errors="replace") as f:
                    content = f.read()

                count = content.count(old_text)
                if count == 0:
                    logger.info("builtin[edit_file]: old_text not found in %s", path)
                    return json.dumps({
                        "error": "old_text not found in the file. Make sure it matches exactly (including whitespace/indentation).",
                    })
                if count > 1:
                    logger.info("builtin[edit_file]: old_text matched %d times in %s", count, path)
                    return json.dumps({
                        "error": f"old_text matched {count} times — it must be unique. Include more surrounding context to make it unique.",
                    })

                new_content = content.replace(old_text, new_text, 1)
                with open(real_path, "w") as f:
                    f.write(new_content)
                # Update file cache
                self.file_cache.put(real_path, new_content)
                logger.info("builtin[edit_file]: %s — replaced %d chars with %d chars", path, len(old_text), len(new_text))
                return json.dumps({
                    "status": "ok",
                    "path": path,
                    "chars_removed": len(old_text),
                    "chars_added": len(new_text),
                })

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

                # The LLM only knows about relative paths inside the repo.
                # Handle cd prefixes so the LLM doesn't get stuck in a loop.
                effective_cwd = repo_dir
                actual_command = command.strip()

                import re as _re

                # Intercept bare "cd <path>" (no &&/;) — useless in subprocess
                bare_cd = _re.match(r'^cd(\s+\S+)?\s*$', actual_command)
                if bare_cd:
                    logger.info("builtin[run_command]: intercepted bare cd")
                    return json.dumps({
                        "exit_code": 0,
                        "output": "Your working directory is already the repo root. "
                                  "Do not use 'cd' alone. To run in a subdirectory: cd subdir && your_command",
                    })

                # Handle "cd <path> && <rest>" — resolve relative path as cwd
                cd_prefix = _re.match(r'^cd\s+([^\s;&]+)\s*&&\s*(.+)$', actual_command, _re.DOTALL)
                if cd_prefix:
                    cd_target = cd_prefix.group(1)
                    actual_command = cd_prefix.group(2).strip()
                    # Only allow relative paths within the repo
                    if os.path.isabs(cd_target):
                        logger.warning("builtin[run_command]: blocked absolute cd path: %s", cd_target)
                        return json.dumps({
                            "exit_code": 1,
                            "output": "Absolute paths are not allowed. Use relative paths from the repo root.",
                        })
                    resolved = os.path.realpath(os.path.join(repo_dir, cd_target))
                    real_repo = os.path.realpath(repo_dir)
                    if not resolved.startswith(real_repo):
                        logger.warning("builtin[run_command]: cd traversal blocked: %s", cd_target)
                        return json.dumps({
                            "exit_code": 1,
                            "output": "Path traversal not allowed. Stay within the repo directory.",
                        })
                    if os.path.isdir(resolved):
                        effective_cwd = resolved
                    logger.info("builtin[run_command]: cd to subdir=%s cmd=%s", cd_target, actual_command[:100])

                logger.info("builtin[run_command]: cmd=%s cwd=%s", actual_command[:200], effective_cwd)
                proc = await asyncio.create_subprocess_shell(
                    actual_command,
                    cwd=effective_cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                try:
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
                    output = stdout.decode(errors="replace")
                    if len(output) > 50_000:
                        logger.info("builtin[run_command]: output truncated from %d to 50000 chars", len(output))
                        output = output[:50_000] + "\n... (truncated)"
                    # Strip absolute repo path from output so LLM never sees it
                    if repo_dir:
                        output = output.replace(repo_dir + "/", "").replace(repo_dir, ".")
                    logger.info("builtin[run_command]: exit_code=%d output_len=%d cmd=%s",
                                proc.returncode, len(output), actual_command[:100])
                    return json.dumps({
                        "exit_code": proc.returncode,
                        "output": output,
                    })
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.warning("builtin[run_command]: timed out after 120s cmd=%s", actual_command[:200])
                    return json.dumps({"error": "Command timed out after 120s"})

            elif tool_name == "task_complete":
                summary = tool_input.get("summary", "")
                logger.info("builtin[task_complete]: agent signaled done: %s", summary[:200])
                return json.dumps({"status": "acknowledged", "summary": summary})

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

        # streamable-http: POST with streaming reads (servers may return SSE streams)
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=30.0), verify=False,
        ) as client:
            # Initialize
            init_resp, init_headers = await self._streamable_post(client, url, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-03-26",
                    "capabilities": {},
                    "clientInfo": {"name": "agents-platform", "version": "0.1.0"},
                },
            }, headers)
            if "error" in init_resp:
                return json.dumps({"error": f"MCP init error: {init_resp['error']}"})

            # Capture session ID for subsequent requests (MCP spec requirement)
            session_id = init_headers.get("mcp-session-id")
            if session_id:
                headers = {**headers, "mcp-session-id": session_id}
                logger.info("MCP streamable-http: session_id=%s", session_id)

            # Initialized notification (fire-and-forget)
            try:
                await self._streamable_post(client, url, {
                    "jsonrpc": "2.0", "method": "notifications/initialized",
                }, headers, expect_response=False)
            except Exception:
                pass

            # Call tool
            result, _ = await self._streamable_post(client, url, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": tool_name, "arguments": tool_input},
            }, headers, timeout=55)

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
            logger.info("SSE: connecting to %s", url)
            req = client.build_request("GET", url, headers={"Accept": "text/event-stream"})
            resp = await client.send(req, stream=True)
            resp.raise_for_status()
            reader = asyncio.create_task(_sse_reader(resp))

            try:
                # Wait for endpoint
                etype, edata = await asyncio.wait_for(events.get(), timeout=15)
                logger.info("SSE: first event type=%s data=%s", etype, edata[:200] if edata else "")
                if etype != "endpoint":
                    return json.dumps({"error": f"Expected endpoint event, got {etype}: {edata}"})
                post_url = edata if edata.startswith("http") else base_url + edata
                logger.info("SSE: post_url=%s", post_url)

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
    def _fallback_transport(transport: str, url: str) -> tuple[str | None, str]:
        """Determine alternate transport/URL to try when primary fails."""
        from urllib.parse import urlparse, urlunparse
        parsed = urlparse(url)
        base_url = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
        if transport == "sse":
            return "streamable-http", base_url + "/mcp"
        elif transport == "streamable-http":
            return "sse", base_url + "/sse"
        return None, url

    @staticmethod
    async def _streamable_post(
        client, url: str, body: dict, headers: dict, *,
        expect_response: bool = True, timeout: float = 30,
    ) -> tuple[dict, dict]:
        """POST a JSON-RPC message, handling both JSON and SSE-stream responses.

        Returns (json_response, response_headers_dict).
        Servers may return text/event-stream for streamable-HTTP POSTs.
        Uses streaming reads to avoid hanging on open SSE connections.
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
