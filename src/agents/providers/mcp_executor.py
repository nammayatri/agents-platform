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

import re as _re_module

import asyncpg

# Dangerous command patterns — checked before shell execution in run_command.
# Each tuple: (regex_pattern, human-readable reason).
_BLOCKED_COMMANDS: list[tuple[str, str]] = [
    (r'\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|.*--recursive)\b', "Recursive rm blocked"),
    (r'\bgit\s+push\b(?!.*--dry-run)', "git push blocked (use --dry-run to preview)"),
    (r'\bgit\s+reset\s+--hard\b', "git reset --hard blocked"),
    (r'\bcurl\b.*\|\s*(bash|sh|zsh)\b', "Piping curl to shell blocked"),
    (r'\bwget\b.*\|\s*(bash|sh|zsh)\b', "Piping wget to shell blocked"),
    (r'\bsudo\b', "sudo blocked"),
    (r'\bnpm\s+publish\b', "npm publish blocked"),
    (r'\bchmod\s+777\b', "chmod 777 blocked"),
    (r'\bmkfs\b', "mkfs blocked"),
    (r'\bdd\s+if=', "dd blocked"),
]


def _analyze_command_safety(command: str, repo_dir: str) -> dict:
    """Analyze a shell command for safety risks beyond the basic blocklist.

    Returns:
        dict with "risk_level" ("safe"|"moderate"|"dangerous") and "reason" (str).
    """
    import re

    reasons: list[str] = []
    risk = "safe"

    # Pipe chain analysis — detect data piped to interpreters
    pipe_to_exec = re.search(
        r'\|\s*(python[23]?|ruby|perl|node|php|bash|sh|zsh|eval)\b',
        command, re.IGNORECASE,
    )
    if pipe_to_exec:
        reasons.append(f"Pipe to interpreter: {pipe_to_exec.group()}")
        risk = "dangerous"

    # Destructive file operations
    destructive_patterns = [
        (r'\bfind\b.*-delete\b', "find -delete"),
        (r'\bfind\b.*-exec\s+rm\b', "find -exec rm"),
        (r'>\s*/dev/sd[a-z]', "Write to block device"),
        (r'>\s*\.(env|gitignore|dockerignore|git)', "Overwrite config file"),
        (r'\btruncate\b', "truncate command"),
        (r'\bshred\b', "shred command"),
    ]
    for pattern, desc in destructive_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            reasons.append(f"Destructive operation: {desc}")
            risk = "dangerous"

    # Network exfiltration patterns
    network_patterns = [
        (r'\bcurl\b.*(-X\s*POST|-d\s)', "curl POST/data upload", "moderate"),
        (r'\bwget\b.*--post', "wget POST", "moderate"),
        (r'\bnc\b|\bnetcat\b', "netcat usage", "dangerous"),
        (r'\bscp\b|\brsync\b.*:', "Remote file transfer", "moderate"),
        (r'\bssh\b\s', "SSH command", "moderate"),
    ]
    for pattern, desc, level in network_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            reasons.append(f"Network operation: {desc}")
            if level == "dangerous" or risk != "dangerous":
                risk = max(risk, level, key=lambda x: {"safe": 0, "moderate": 1, "dangerous": 2}[x])

    # Privilege escalation
    priv_patterns = [
        (r'\bchown\b', "chown"),
        (r'\bchmod\b\s+[0-7]*[67][0-7]{2}', "chmod with broad permissions"),
        (r'\bsetuid\b|\bsetgid\b', "setuid/setgid"),
    ]
    for pattern, desc in priv_patterns:
        if re.search(pattern, command, re.IGNORECASE):
            reasons.append(f"Privilege change: {desc}")
            if risk == "safe":
                risk = "moderate"

    # Path escape — commands targeting outside repo_dir
    if repo_dir:
        outside_patterns = [
            r'(?:^|\s)/etc/',
            r'(?:^|\s)/usr/',
            r'(?:^|\s)/var/',
            r'(?:^|\s)/tmp/',
            r'(?:^|\s)~/',
            r'(?:^|\s)\$HOME/',
        ]
        for pattern in outside_patterns:
            if re.search(pattern, command):
                reasons.append("Targets paths outside repo")
                if risk == "safe":
                    risk = "moderate"
                break

    reason = "; ".join(reasons) if reasons else "No issues detected"
    return {"risk_level": risk, "reason": reason}


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
        logger.debug("execute_tool: %s args=%s", tool_name, {k: (v[:80] + "..." if isinstance(v, str) and len(v) > 80 else v) for k, v in tool_input.items()})

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

    async def execute_tools_batch(
        self,
        tool_calls: list[dict],
        mcp_tools: list[dict],
        *,
        max_parallel: int = 5,
    ) -> list[str]:
        """Execute multiple tool calls concurrently with bounded parallelism.

        Args:
            tool_calls: List of dicts with "name" and "arguments" keys.
            mcp_tools: The resolved tools list (with _mcp_server_id or _builtin metadata).
            max_parallel: Max concurrent tool executions (default 5).

        Returns:
            List of result strings in the same order as input tool_calls.
        """
        sem = asyncio.Semaphore(max_parallel)

        async def _guarded(tc: dict) -> str:
            async with sem:
                return await self.execute_tool(
                    tc["name"], tc.get("arguments", {}), mcp_tools,
                )

        results = await asyncio.gather(
            *[_guarded(tc) for tc in tool_calls],
            return_exceptions=True,
        )

        # Convert exceptions to error JSON strings
        processed: list[str] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                tc_name = tool_calls[i].get("name", "unknown")
                logger.error("batch_exec: tool %s failed: %s", tc_name, r)
                processed.append(json.dumps({
                    "error": f"Tool execution failed: {r}",
                }))
            else:
                processed.append(r)

        return processed

    def _get_file_manager(self, workspace_path: str):
        """Get or create a WorkspaceFileManager for the given workspace."""
        from agents.orchestrator.file_manager import WorkspaceFileManager
        # Cache per workspace_path to avoid re-computing on every tool call
        if not hasattr(self, "_fm_cache"):
            self._fm_cache = {}
        if workspace_path not in self._fm_cache:
            self._fm_cache[workspace_path] = WorkspaceFileManager(workspace_path)
        return self._fm_cache[workspace_path]

    async def _execute_builtin(self, tool_meta: dict, tool_input: dict) -> str:
        """Execute a built-in workspace tool directly (no MCP server)."""
        workspace_path = tool_meta.get("_workspace_path", "")
        repo_dir = workspace_path
        tool_name = tool_meta["name"]
        fm = self._get_file_manager(workspace_path) if workspace_path else None

        logger.debug("builtin[%s]: workspace=%s", tool_name, workspace_path)

        try:
            if tool_name == "read_file":
                path = tool_input.get("path", "")
                real_path = fm.resolve(path) if fm else os.path.realpath(os.path.join(repo_dir, path))
                if fm and not fm.can_read(real_path):
                    logger.warning("builtin[read_file]: access denied: %s", path)
                    return json.dumps({"error": f"Access denied: {path}"})
                if not os.path.isfile(real_path):
                    logger.info("builtin[read_file]: file not found: %s (resolved: %s)", path, real_path)
                    return json.dumps({"error": f"File not found: {path}"})

                repo_label = fm.identify(real_path) if fm else "main"

                # Try cache first (validates mtime freshness automatically)
                content = self.file_cache.get(real_path)
                if content is not None:
                    # Apply offset/limit slicing if requested
                    offset = tool_input.get("offset", 0)
                    limit = tool_input.get("limit")
                    if offset > 0 or limit is not None:
                        all_lines = content.split("\n")
                        total_lines = len(all_lines)
                        end = min(offset + limit, total_lines) if limit else total_lines
                        sliced = all_lines[offset:end]
                        sliced_content = "\n".join(sliced)
                        line_header = f"[Lines {offset + 1}-{min(end, total_lines)} of {total_lines}]"
                        if end < total_lines:
                            remaining = total_lines - end
                            sliced_content += f"\n\n... ({remaining} more lines. Use read_file with offset={end} to continue reading.)"
                        logger.debug("builtin[read_file]: %s → lines %d-%d of %d (cache hit, repo=%s)", path, offset + 1, min(end, total_lines), total_lines, repo_label)
                        return f"[Repository: {repo_label}] {path} {line_header}\n{sliced_content}"
                    logger.debug("builtin[read_file]: %s → %d chars (cache hit, repo=%s)", path, len(content), repo_label)
                    return f"[Repository: {repo_label}] {path}\n{content}"

                # Cache miss — read from disk
                with open(real_path, "r", errors="replace") as f:
                    content = f.read()

                # Cache full content (before any slicing/truncation)
                full_char_count = len(content)
                if full_char_count <= 100_000:
                    self.file_cache.put(real_path, content)

                # Apply offset/limit slicing if requested
                offset = tool_input.get("offset", 0)
                limit = tool_input.get("limit")
                all_lines = content.split("\n")
                total_lines = len(all_lines)

                if offset > 0 or limit is not None:
                    end = min(offset + limit, total_lines) if limit else total_lines
                    sliced = all_lines[offset:end]
                    content = "\n".join(sliced)
                    line_header = f"[Lines {offset + 1}-{min(end, total_lines)} of {total_lines}]"
                    if end < total_lines:
                        remaining = total_lines - end
                        content += f"\n\n... ({remaining} more lines. Use read_file with offset={end} to continue reading.)"
                    logger.debug("builtin[read_file]: %s → lines %d-%d of %d (repo=%s)", path, offset + 1, min(end, total_lines), total_lines, repo_label)
                    return f"[Repository: {repo_label}] {path} {line_header}\n{content}"

                # Truncate very large files with informative message
                if full_char_count > 100_000:
                    kept_lines = []
                    char_count = 0
                    for line in all_lines:
                        if char_count + len(line) + 1 > 100_000:
                            break
                        kept_lines.append(line)
                        char_count += len(line) + 1
                    remaining = total_lines - len(kept_lines)
                    content = "\n".join(kept_lines)
                    content += (
                        f"\n\n... ({remaining} more lines not shown, {full_char_count - char_count} more chars. "
                        f"Use read_file with offset={len(kept_lines)} to continue reading.)"
                    )
                    logger.debug("builtin[read_file]: %s truncated — showing %d of %d lines (repo=%s)", path, len(kept_lines), total_lines, repo_label)

                logger.debug("builtin[read_file]: %s → %d chars (disk, repo=%s)", path, len(content), repo_label)
                return f"[Repository: {repo_label}] {path} [{total_lines} lines total]\n{content}"

            elif tool_name == "write_file":
                path = tool_input.get("path", "")
                content = tool_input.get("content", "")
                real_path = fm.resolve(path) if fm else os.path.realpath(os.path.join(repo_dir, path))
                if fm and not fm.can_write(real_path):
                    logger.warning("builtin[write_file]: access denied: %s", path)
                    return json.dumps({"error": f"Write access denied: {path}"})
                os.makedirs(os.path.dirname(real_path), exist_ok=True)
                with open(real_path, "w") as f:
                    f.write(content)
                # Update cache with the written content (mtime from disk after write)
                self.file_cache.put(real_path, content)
                repo_label = fm.identify(real_path) if fm else "main"
                logger.debug("builtin[write_file]: %s → %d bytes written (repo=%s)", path, len(content), repo_label)
                return json.dumps({"status": "ok", "path": path, "bytes_written": len(content), "repository": repo_label})

            elif tool_name == "edit_file":
                path = tool_input.get("path", "")
                old_text = tool_input.get("old_text", "")
                new_text = tool_input.get("new_text", "")
                if not old_text:
                    return json.dumps({"error": "old_text is required"})
                real_path = fm.resolve(path) if fm else os.path.realpath(os.path.join(repo_dir, path))
                if fm and not fm.can_write(real_path):
                    logger.warning("builtin[edit_file]: access denied: %s", path)
                    return json.dumps({"error": f"Write access denied: {path}"})
                if not os.path.isfile(real_path):
                    logger.info("builtin[edit_file]: file not found: %s", path)
                    return json.dumps({"error": f"File not found: {path}"})

                with open(real_path, "r", errors="replace") as f:
                    content = f.read()

                # Use progressive fuzzy matching instead of exact-only matching
                try:
                    from agents.utils.edit_match import apply_edit, EditMatchError
                    try:
                        new_content, match = apply_edit(content, old_text, new_text)
                        with open(real_path, "w") as f:
                            f.write(new_content)
                        self.file_cache.put(real_path, new_content)
                        logger.debug(
                            "builtin[edit_file]: %s — replaced %d chars with %d chars (method=%s, confidence=%.2f)",
                            path, len(old_text), len(new_text), match.method, match.confidence,
                        )
                        repo_label = fm.identify(real_path) if fm else "main"
                        return json.dumps({
                            "status": "ok",
                            "path": path,
                            "chars_removed": len(match.matched_text),
                            "chars_added": len(new_text),
                            "match_method": match.method,
                            "match_confidence": round(match.confidence, 3),
                            "repository": repo_label,
                        })
                    except EditMatchError as e:
                        logger.debug("builtin[edit_file]: fuzzy match failed in %s: %s", path, str(e)[:200])
                        return json.dumps({"error": str(e)})
                except ImportError:
                    # Fallback to exact matching if edit_match module not available
                    count = content.count(old_text)
                    if count == 0:
                        logger.debug("builtin[edit_file]: old_text not found in %s", path)
                        return json.dumps({
                            "error": "old_text not found in the file. Make sure it matches exactly (including whitespace/indentation).",
                        })
                    if count > 1:
                        logger.debug("builtin[edit_file]: old_text matched %d times in %s", count, path)
                        return json.dumps({
                            "error": f"old_text matched {count} times — it must be unique. Include more surrounding context to make it unique.",
                        })
                    new_content = content.replace(old_text, new_text, 1)
                    with open(real_path, "w") as f:
                        f.write(new_content)
                    self.file_cache.put(real_path, new_content)
                    repo_label = fm.identify(real_path) if fm else "main"
                    logger.debug("builtin[edit_file]: %s — replaced %d chars with %d chars (repo=%s)", path, len(old_text), len(new_text), repo_label)
                    return json.dumps({
                        "status": "ok",
                        "path": path,
                        "chars_removed": len(old_text),
                        "chars_added": len(new_text),
                        "repository": repo_label,
                    })

            elif tool_name == "list_directory":
                path = tool_input.get("path", "")
                real_path = fm.resolve(path) if fm and path else (fm.repo_dir if fm else repo_dir)
                if fm and not fm.can_read(real_path):
                    logger.warning("builtin[list_directory]: access denied: %s", path)
                    return json.dumps({"error": f"Access denied: {path}"})
                if not os.path.isdir(real_path):
                    logger.info("builtin[list_directory]: directory not found: %s (resolved: %s)", path, real_path)
                    return json.dumps({"error": f"Directory not found: {path}"})
                repo_label = fm.identify(real_path) if fm else "main"
                entries = sorted(os.listdir(real_path))
                result = []
                for e in entries[:500]:  # limit entries
                    full_e = os.path.join(real_path, e)
                    result.append({
                        "name": e,
                        "type": "directory" if os.path.isdir(full_e) else "file",
                    })
                logger.debug("builtin[list_directory]: %s → %d entries (repo=%s)", path or "/", len(result), repo_label)
                return json.dumps({"repository": repo_label, "path": path or "/", "entries": result})

            elif tool_name == "search_files":
                pattern = tool_input.get("pattern", "")
                if not pattern:
                    return json.dumps({"error": "No search pattern provided"})
                search_path = tool_input.get("path", "")
                file_glob = tool_input.get("file_glob", "*")
                real_search = fm.resolve(search_path) if fm and search_path else (fm.repo_dir if fm else repo_dir)
                if fm and not fm.can_read(real_search):
                    logger.warning("builtin[search_files]: access denied: %s", search_path)
                    return json.dumps({"error": f"Access denied: {search_path}"})
                if not os.path.isdir(real_search):
                    logger.info("builtin[search_files]: directory not found: %s", search_path)
                    return json.dumps({"error": f"Directory not found: {search_path}"})

                repo_label = fm.identify(real_search) if fm else "main"
                logger.debug("builtin[search_files]: pattern=%s path=%s glob=%s repo=%s", pattern, search_path or "/", file_glob, repo_label)
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
                    if not output:
                        return f"[Repository: {repo_label}] No matches found."

                    # Apply offset/max_results pagination
                    max_results = tool_input.get("max_results", 200)
                    skip = tool_input.get("offset", 0)
                    lines = [l for l in output.strip().split("\n") if l.strip()]
                    total_matches = len(lines)
                    lines = lines[skip:skip + max_results]
                    output = "\n".join(lines)

                    if skip + max_results < total_matches:
                        remaining = total_matches - skip - max_results
                        output += (
                            f"\n\n... ({remaining} more matches not shown. "
                            f"Use search_files with offset={skip + max_results} to see next page.)"
                        )

                    showing = f" (showing {skip + 1}-{skip + len(lines)} of {total_matches})" if skip > 0 or total_matches > len(lines) else ""
                    logger.debug("builtin[search_files]: %d total matches, showing %d%s", total_matches, len(lines), f" (offset={skip})" if skip else "")
                    header = f"[Repository: {repo_label}] Search results for '{pattern}'{showing}:\n"
                    return header + output
                except asyncio.CancelledError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                    logger.debug("builtin[search_files]: cancelled, subprocess killed")
                    raise
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
                    logger.debug("builtin[run_command]: intercepted bare cd")
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
                    logger.debug("builtin[run_command]: cd to subdir=%s cmd=%s", cd_target, actual_command[:100])

                # Check against blocked command patterns
                for _bp, _br in _BLOCKED_COMMANDS:
                    if _re.search(_bp, actual_command, _re.IGNORECASE):
                        logger.warning("builtin[run_command]: blocked: %s — %s", actual_command[:200], _br)
                        return json.dumps({"exit_code": 1, "output": f"Command blocked: {_br}"})

                # Safety analysis beyond blocklist
                safety = _analyze_command_safety(actual_command, repo_dir)
                if safety["risk_level"] == "dangerous":
                    logger.warning("builtin[run_command]: DANGEROUS command blocked: %s — %s",
                                   actual_command[:200], safety["reason"])
                    return json.dumps({
                        "exit_code": 1,
                        "output": f"Command blocked (safety analysis): {safety['reason']}",
                    })
                elif safety["risk_level"] == "moderate":
                    logger.debug("builtin[run_command]: moderate risk: %s — %s",
                                actual_command[:200], safety["reason"])

                logger.debug("builtin[run_command]: cmd=%s cwd=%s", actual_command[:200], effective_cwd)
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
                        total_len = len(output)
                        logger.info("builtin[run_command]: output truncated from %d to 50000 chars", total_len)
                        output = output[:50_000] + (
                            f"\n\n... (output truncated at 50K of {total_len} chars. "
                            f"Pipe through head/tail/grep to get specific sections.)"
                        )
                    # Strip absolute repo path from output so LLM never sees it
                    if repo_dir:
                        output = output.replace(repo_dir + "/", "").replace(repo_dir, ".")
                    repo_label = fm.identify(os.path.realpath(repo_dir)) if fm else "main"
                    logger.debug("builtin[run_command]: exit_code=%d output_len=%d cmd=%s repo=%s",
                                proc.returncode, len(output), actual_command[:100], repo_label)
                    return json.dumps({
                        "exit_code": proc.returncode,
                        "output": output,
                        "repository": repo_label,
                    })
                except asyncio.CancelledError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    await proc.wait()
                    logger.debug("builtin[run_command]: cancelled, subprocess killed cmd=%s", actual_command[:100])
                    raise
                except asyncio.TimeoutError:
                    proc.kill()
                    logger.warning("builtin[run_command]: timed out after 120s cmd=%s", actual_command[:200])
                    return json.dumps({"error": "Command timed out after 120s"})

            elif tool_name == "semantic_search":
                query = tool_input.get("query", "")
                top_k = tool_input.get("top_k", 10)
                if not query:
                    return json.dumps({"error": "query is required"})
                try:
                    # Use shared project-level index directory if available
                    index_dir = tool_meta.get("_index_dir")
                    dep_index_dirs = tool_meta.get("_dep_index_dirs")

                    if dep_index_dirs:
                        from agents.indexing.search_tool import execute_multi_repo_search
                        result_text, meta = execute_multi_repo_search(
                            repo_dir, query, top_k=top_k,
                            cache_dir=index_dir, dep_index_dirs=dep_index_dirs,
                        )
                    else:
                        from agents.indexing.search_tool import execute_semantic_search
                        result_text, meta = execute_semantic_search(
                            repo_dir, query, top_k=top_k, cache_dir=index_dir,
                        )
                    logger.debug(
                        "builtin[semantic_search]: query=%s top_k=%d results=%d top_score=%.3f latency=%dms source=%s",
                        query[:100], top_k,
                        meta.get("results_count", 0),
                        meta.get("top_score", 0),
                        meta.get("latency_ms", 0),
                        meta.get("source", "?"),
                    )
                    return result_text
                except ImportError:
                    logger.debug("builtin[semantic_search]: module not available, falling back to search_files hint")
                    return json.dumps({
                        "error": "Semantic search is not available (missing dependencies). Use search_files tool instead.",
                    })
                except Exception as e:
                    logger.warning("builtin[semantic_search]: failed: %s", e)
                    return json.dumps({"error": f"Semantic search failed: {e}. Use search_files instead."})

            elif tool_name == "web_search":
                query = tool_input.get("query", "")
                max_results = min(tool_input.get("max_results", 5), 10)
                if not query:
                    return "Error: query is required"
                try:
                    from duckduckgo_search import DDGS
                    with DDGS() as ddgs:
                        results = list(ddgs.text(query, max_results=max_results))
                    if not results:
                        return f"No results found for: {query}"
                    lines = []
                    for i, r in enumerate(results, 1):
                        lines.append(f"{i}. **{r.get('title', 'Untitled')}**")
                        lines.append(f"   URL: {r.get('href', '')}")
                        lines.append(f"   {r.get('body', '')}")
                        lines.append("")
                    return "\n".join(lines)
                except ImportError:
                    return "Error: duckduckgo-search package not installed. Run: pip install duckduckgo-search"
                except Exception as e:
                    return f"Search failed: {e}"

            elif tool_name == "web_fetch":
                url = tool_input.get("url", "")
                if not url:
                    return "Error: url is required"
                try:
                    import httpx
                    jina_url = f"https://r.jina.ai/{url}"
                    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                        resp = await client.get(jina_url, headers={
                            "Accept": "text/markdown",
                            "X-No-Cache": "true",
                        })
                        resp.raise_for_status()
                        content = resp.text
                        if len(content) > 15000:
                            content = content[:15000] + "\n\n[... truncated, page too long]"
                        return content
                except Exception as e:
                    return f"Failed to fetch {url}: {e}"

            elif tool_name == "task_complete":
                summary = tool_input.get("summary", "")
                logger.debug("builtin[task_complete]: agent signaled done: %s", summary[:200])
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
                logger.debug("MCP streamable-http: session_id=%s", session_id)

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
            logger.debug("SSE: connecting to %s", url)
            req = client.build_request("GET", url, headers={"Accept": "text/event-stream"})
            resp = await client.send(req, stream=True)
            resp.raise_for_status()
            reader = asyncio.create_task(_sse_reader(resp))

            try:
                # Wait for endpoint
                etype, edata = await asyncio.wait_for(events.get(), timeout=15)
                logger.debug("SSE: first event type=%s data=%s", etype, edata[:200] if edata else "")
                if etype != "endpoint":
                    return json.dumps({"error": f"Expected endpoint event, got {etype}: {edata}"})
                post_url = edata if edata.startswith("http") else base_url + edata
                logger.debug("SSE: post_url=%s", post_url)

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
