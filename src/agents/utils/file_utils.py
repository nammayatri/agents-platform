"""Shared file utility functions for workspace file operations.

Consolidates duplicated file tree building, language detection, and binary
detection that previously existed in both `api/routes/todos.py` and
`orchestrator/workspace.py`.
"""

import os

# Directories to skip when building file trees
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".next", ".nuxt",
    "coverage", ".eggs", "*.egg-info", "target",
}

# File extensions treated as binary (not readable as text)
BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".svg", ".webp",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".mp3", ".mp4", ".avi",
    ".mov", ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".exe",
    ".dll", ".so", ".dylib", ".pyc", ".pyo", ".class", ".jar", ".war",
}

# Extension → Monaco editor language ID
LANGUAGE_MAP: dict[str, str] = {
    ".py": "python", ".pyw": "python", ".pyi": "python",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".json": "json", ".jsonc": "json",
    ".html": "html", ".htm": "html",
    ".css": "css", ".scss": "scss", ".sass": "scss", ".less": "less",
    ".md": "markdown", ".mdx": "markdown",
    ".yaml": "yaml", ".yml": "yaml",
    ".toml": "toml",
    ".xml": "xml", ".xsl": "xml",
    ".sql": "sql",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift",
    ".c": "c", ".h": "c",
    ".cpp": "cpp", ".cxx": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".r": "r",
    ".lua": "lua",
    ".pl": "perl", ".pm": "perl",
    ".ex": "elixir", ".exs": "elixir",
    ".erl": "erlang",
    ".hs": "haskell",
    ".scala": "scala",
    ".clj": "clojure",
    ".dart": "dart",
    ".vim": "vim",
    ".dockerfile": "dockerfile",
    ".graphql": "graphql", ".gql": "graphql",
    ".proto": "protobuf",
    ".tf": "hcl",
    ".ini": "ini", ".cfg": "ini",
    ".env": "dotenv",
    ".txt": "plaintext", ".log": "plaintext",
    ".csv": "plaintext",
    ".makefile": "makefile",
}

MAX_FILE_SIZE = 500_000  # 500KB


def detect_language(file_path: str) -> str:
    """Detect the Monaco editor language ID for a file path."""
    name = os.path.basename(file_path).lower()
    if name == "dockerfile":
        return "dockerfile"
    if name == "makefile":
        return "makefile"
    ext = os.path.splitext(name)[1]
    return LANGUAGE_MAP.get(ext, "plaintext")


def is_binary(file_path: str) -> bool:
    """Check if a file is binary based on its extension."""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in BINARY_EXTENSIONS


def build_file_tree(
    base_dir: str,
    current_dir: str,
    max_depth: int = 8,
    depth: int = 0,
) -> list[dict]:
    """Build a structured JSON file tree for a directory.

    Returns a list of dicts with keys: name, path, type ('dir'|'file'),
    size (files only), children (dirs only). Directories come first,
    both sorted alphabetically.
    """
    if depth >= max_depth:
        return []

    result: list[dict] = []
    try:
        entries = sorted(os.listdir(current_dir))
    except PermissionError:
        return []

    dirs = []
    files = []
    for entry in entries:
        if entry.startswith(".") and entry not in (".env", ".env.example"):
            if entry in (".git",):
                continue
        if entry in SKIP_DIRS:
            continue
        full_path = os.path.join(current_dir, entry)
        rel_path = os.path.relpath(full_path, base_dir)
        if os.path.isdir(full_path):
            children = build_file_tree(base_dir, full_path, max_depth, depth + 1)
            dirs.append({
                "name": entry,
                "path": rel_path,
                "type": "dir",
                "children": children,
            })
        else:
            try:
                size = os.path.getsize(full_path)
            except OSError:
                size = 0
            files.append({
                "name": entry,
                "path": rel_path,
                "type": "file",
                "size": size,
            })

    return dirs + files


def build_file_tree_text(
    repo_dir: str,
    max_depth: int = 5,
) -> str:
    """Build a plain-text directory tree string for display.

    Used by the orchestrator for providing file tree context to LLMs.
    """
    if not os.path.isdir(repo_dir):
        return ""

    lines: list[str] = []

    def _walk(path: str, prefix: str, depth: int) -> None:
        if depth > max_depth:
            lines.append(f"{prefix}...")
            return
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return
        dirs = [e for e in entries if os.path.isdir(os.path.join(path, e)) and e not in SKIP_DIRS]
        files = [e for e in entries if os.path.isfile(os.path.join(path, e))]
        for f in files:
            lines.append(f"{prefix}{f}")
        for d in dirs:
            lines.append(f"{prefix}{d}/")
            _walk(os.path.join(path, d), prefix + "  ", depth + 1)

    _walk(repo_dir, "", 0)
    return "\n".join(lines)


def validate_workspace_path(repo_dir: str, file_path: str) -> str:
    """Validate and resolve a file path within a workspace, preventing traversal.

    Raises ValueError if the path attempts to escape the workspace.
    Returns the resolved absolute path.
    """
    if ".." in file_path:
        raise ValueError("Path traversal not allowed")
    full_path = os.path.normpath(os.path.join(repo_dir, file_path))
    real_path = os.path.realpath(full_path)
    real_repo = os.path.realpath(repo_dir)
    if not real_path.startswith(real_repo + os.sep) and real_path != real_repo:
        raise ValueError("Path outside workspace")
    return full_path
