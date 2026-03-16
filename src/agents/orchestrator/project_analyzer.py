"""Project analyzer: clones repo, smart-samples the codebase, and builds LLM understanding.

When a project is created or updated with a repo_url, this module:
1. Clones the repository (and dependencies) into a project workspace
2. Smart-samples the codebase: directory tree + docs + config + entry points + key source files
3. Sends the sampled content to an LLM for structured analysis
4. Stores the understanding in settings_json["project_understanding"]
"""

import asyncio
import json
import logging
import os

import asyncpg

from agents.config.settings import settings
from agents.orchestrator.workspace import WorkspaceManager
from agents.providers.registry import ProviderRegistry
from agents.schemas.agent import LLMMessage

logger = logging.getLogger(__name__)

# Max total chars of file content to include in the prompt
FILE_BUDGET = 100_000

# Files always worth reading (config/manifest)
CONFIG_FILES = {
    "package.json", "package-lock.json", "yarn.lock",
    "pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "Pipfile",
    "cargo.toml", "cargo.lock",
    "go.mod", "go.sum",
    "gemfile", "gemfile.lock",
    "pom.xml", "build.gradle", "build.gradle.kts",
    "makefile", "cmake", "cmakelists.txt",
    "dockerfile", "docker-compose.yml", "docker-compose.yaml",
    ".env.example", ".env.sample",
    "tsconfig.json", "vite.config.ts", "vite.config.js",
    "webpack.config.js", "webpack.config.ts",
    "tailwind.config.js", "tailwind.config.ts",
    ".eslintrc.json", ".eslintrc.js", "prettier.config.js",
}

# Common entry point file names
ENTRY_POINTS = {
    "main.py", "app.py", "manage.py", "wsgi.py", "asgi.py",
    "index.ts", "index.js", "main.ts", "main.js", "app.ts", "app.js",
    "server.ts", "server.js",
    "main.rs", "lib.rs",
    "main.go", "cmd/main.go",
    "program.cs", "startup.cs",
}

# Directories to always skip
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", ".next", ".nuxt", "target", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "vendor", "coverage", ".cache", ".turbo",
}

# Binary/generated extensions to skip
SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".so", ".o", ".a", ".dylib", ".dll", ".exe",
    ".wasm", ".min.js", ".min.css", ".map",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".eot",
    ".zip", ".tar", ".gz", ".bz2", ".7z",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx",
    ".lock",  # lock files are usually too large and noisy
}

ANALYZER_SYSTEM_PROMPT = """\
You are a project analyst. Given a project's directory structure, documentation, \
configuration files, and sampled source code, produce a structured understanding.

Your analysis should cover:
1. **Purpose**: What the project does, its core value proposition
2. **Architecture**: High-level architecture, key modules/components, data flow
3. **Tech stack**: Languages, frameworks, databases, infrastructure
4. **Key patterns**: Coding conventions, design patterns, error handling approach
5. **Dependency relationships**: How each listed dependency is used
6. **API surface**: Key endpoints, interfaces, or entry points
7. **Testing**: Testing approach, test frameworks, CI/CD patterns
8. **Important context**: Anything an AI agent should know before working on tasks

Output a JSON object with these keys:
- "purpose": string (2-3 sentences)
- "architecture": string (paragraph describing structure)
- "tech_stack": list of strings
- "key_patterns": list of strings (important conventions)
- "dependency_map": list of {"name": string, "role": string}
- "api_surface": string (key endpoints or interfaces)
- "testing_approach": string
- "important_context": list of strings (things an agent must know)
- "summary": string (1 paragraph executive summary)
"""


class ProjectAnalyzer:
    def __init__(self, db: asyncpg.Pool, redis=None):
        self.db = db
        self.redis = redis
        self.registry = ProviderRegistry(db)
        self.workspace_mgr = WorkspaceManager(db, settings.workspace_root)

    async def _publish_progress(self, project_id: str, step: str, detail: str) -> None:
        """Publish an analysis progress event via Redis pub/sub."""
        if not self.redis:
            return
        try:
            await self.redis.publish(
                f"project:{project_id}:analysis",
                json.dumps({"step": step, "detail": detail}),
            )
        except Exception:
            logger.debug("Failed to publish analysis progress for %s", project_id)

    async def analyze(self, project_id: str) -> dict | None:
        """Analyze a project's codebase and store the understanding."""
        project = await self.db.fetchrow(
            "SELECT * FROM projects WHERE id = $1", project_id
        )
        if not project:
            return None

        repo_url = project.get("repo_url")
        if not repo_url:
            return None

        # Mark as analyzing
        await self._update_settings(project_id, {"analysis_status": "analyzing"})
        await self._publish_progress(project_id, "cloning", "Cloning repository...")

        try:
            # Clone/pull the repo into workspace (deps cloned in parallel internally)
            workspace_path = await self.workspace_mgr.setup_project_workspace(project_id)
            repo_dir = os.path.join(workspace_path, "repo")

            if not os.path.isdir(repo_dir):
                await self._publish_progress(project_id, "failed", "Repository clone failed")
                await self._update_settings(project_id, {"analysis_status": "failed"})
                return None

            await self._publish_progress(project_id, "scanning", "Scanning codebase...")

            # Run sampling and provider resolution in parallel.
            # _smart_sample and _sample_dependencies are sync (local file I/O),
            # so we run them in a thread while provider resolution does async DB queries.
            context_docs = project.get("context_docs") or []
            if isinstance(context_docs, str):
                context_docs = json.loads(context_docs)
            deps_dir = os.path.join(workspace_path, "deps")

            provider_task = asyncio.create_task(self._resolve_provider(project_id))

            sampled = self._smart_sample(repo_dir)
            dep_summaries = self._sample_dependencies(deps_dir, context_docs)

            provider = await provider_task

            if not sampled["files"]:
                await self._publish_progress(project_id, "failed", "No readable files found")
                await self._update_settings(project_id, {
                    "analysis_status": "no_docs",
                    "project_understanding": None,
                })
                return None

            total_kb = sum(len(f["content"]) for f in sampled["files"]) // 1024
            await self._publish_progress(
                project_id, "sampling",
                f"Sampled {len(sampled['files'])} files ({total_kb} KB)",
            )

            if dep_summaries:
                await self._publish_progress(
                    project_id, "dependencies",
                    f"Read docs for {len(dep_summaries)} dependencies",
                )

            if not provider:
                await self._publish_progress(project_id, "failed", "No AI provider configured")
                await self._update_settings(project_id, {"analysis_status": "failed"})
                return None

            await self._publish_progress(project_id, "analyzing", "Running LLM analysis...")

            # Run LLM analysis
            analysis = await self._run_analysis(
                project_name=project["name"],
                project_description=project.get("description") or "",
                file_tree=sampled["tree"],
                files=sampled["files"],
                dependencies=context_docs,
                dep_summaries=dep_summaries,
                provider=provider,
            )

            if analysis:
                await self._update_settings(project_id, {
                    "analysis_status": "complete",
                    "project_understanding": analysis,
                })
                # Also write to workspace for agent reference
                analysis_path = os.path.join(workspace_path, "analysis.json")
                with open(analysis_path, "w") as f:
                    json.dump(analysis, f, indent=2)

                logger.info(
                    "Project %s analyzed: %d files sampled from workspace",
                    project_id, len(sampled["files"]),
                )
                await self._publish_progress(project_id, "complete", "Analysis complete")
                return analysis
            else:
                await self._publish_progress(project_id, "failed", "LLM analysis returned no result")
                await self._update_settings(project_id, {"analysis_status": "failed"})
                return None

        except Exception:
            logger.exception("Failed to analyze project %s", project_id)
            await self._publish_progress(project_id, "failed", "Analysis failed unexpectedly")
            await self._update_settings(project_id, {"analysis_status": "failed"})
            return None

    async def _resolve_provider(self, project_id: str):
        """Resolve the LLM provider for a project (extracted for parallelism)."""
        project = await self.db.fetchrow(
            "SELECT ai_provider_id, owner_id FROM projects WHERE id = $1",
            project_id,
        )
        provider = None
        if project and project.get("ai_provider_id"):
            provider = await self.registry.instantiate(str(project["ai_provider_id"]))
        if not provider and project:
            row = await self.db.fetchrow(
                "SELECT id FROM ai_provider_configs WHERE owner_id = $1 AND is_active = TRUE "
                "ORDER BY created_at ASC LIMIT 1",
                project["owner_id"],
            )
            if row:
                provider = await self.registry.instantiate(str(row["id"]))
        if not provider:
            row = await self.db.fetchrow(
                "SELECT id FROM ai_provider_configs WHERE is_active = TRUE "
                "ORDER BY created_at ASC LIMIT 1"
            )
            if row:
                provider = await self.registry.instantiate(str(row["id"]))
        if not provider:
            logger.warning("No AI provider available for project analysis")
        return provider

    def _smart_sample(self, repo_dir: str) -> dict:
        """Walk the repo and smart-sample files for analysis.

        Returns {"tree": str, "files": [{"path": str, "content": str}, ...]}.
        """
        tree_lines: list[str] = []
        all_files: list[str] = []

        for root, dirs, files in os.walk(repo_dir):
            # Skip unwanted directories
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

            rel_root = os.path.relpath(root, repo_dir)
            depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1

            if depth > 6:
                continue

            indent = "  " * depth
            if rel_root != ".":
                tree_lines.append(f"{indent}{os.path.basename(root)}/")

            for f in sorted(files):
                ext = os.path.splitext(f)[1].lower()
                if ext in SKIP_EXTENSIONS:
                    continue
                rel_path = os.path.join(rel_root, f) if rel_root != "." else f
                tree_lines.append(f"{indent}  {f}")
                all_files.append(rel_path)

        tree_str = "\n".join(tree_lines)

        # Categorize and prioritize files
        md_files: list[str] = []
        config_files: list[str] = []
        entry_files: list[str] = []
        source_files: list[str] = []

        for fp in all_files:
            basename = os.path.basename(fp).lower()
            ext = os.path.splitext(fp)[1].lower()

            if ext == ".md":
                md_files.append(fp)
            elif basename in CONFIG_FILES:
                config_files.append(fp)
            elif basename in ENTRY_POINTS or fp.lower() in ENTRY_POINTS:
                entry_files.append(fp)
            elif ext in (".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".java", ".cs", ".rb"):
                source_files.append(fp)

        # Prioritize source files: prefer shallow depth, src/lib/app directories
        def _source_priority(path: str) -> tuple:
            depth = path.count(os.sep)
            parts = path.lower().split(os.sep)
            in_key_dir = any(p in ("src", "lib", "app", "api", "routes", "services", "models", "core") for p in parts)
            is_test = any(p in ("test", "tests", "__tests__", "spec") for p in parts) or "test" in os.path.basename(path).lower()
            return (not in_key_dir, is_test, depth, path)

        source_files.sort(key=_source_priority)

        # Build the file list within budget
        sampled_files: list[dict] = []
        total_chars = 0

        def _add_file(rel_path: str, label: str) -> bool:
            nonlocal total_chars
            abs_path = os.path.join(repo_dir, rel_path)
            try:
                content = open(abs_path, "r", errors="replace").read()
            except (OSError, UnicodeDecodeError):
                return False

            if len(content) > 15_000:
                content = content[:15_000] + "\n\n[... truncated]"

            if total_chars + len(content) > FILE_BUDGET:
                return False

            sampled_files.append({"path": rel_path, "content": content, "category": label})
            total_chars += len(content)
            return True

        # Add in priority order
        for f in md_files:
            _add_file(f, "documentation")
        for f in config_files:
            _add_file(f, "config")
        for f in entry_files:
            _add_file(f, "entry_point")
        for f in source_files[:30]:
            if total_chars >= FILE_BUDGET:
                break
            _add_file(f, "source")

        return {"tree": tree_str, "files": sampled_files}

    def _sample_dependencies(
        self, deps_dir: str, context_docs: list[dict]
    ) -> list[dict]:
        """Read README and config from cloned dependency repos."""
        summaries: list[dict] = []
        if not os.path.isdir(deps_dir):
            return summaries

        for dep in context_docs:
            dep_name = dep.get("name", "").replace("/", "_").replace(" ", "_")
            dep_dir = os.path.join(deps_dir, dep_name)
            if not os.path.isdir(dep_dir):
                continue

            summary = {"name": dep.get("name", dep_name)}

            # Read README
            for readme_name in ("README.md", "readme.md", "README", "README.rst"):
                readme_path = os.path.join(dep_dir, readme_name)
                if os.path.isfile(readme_path):
                    try:
                        content = open(readme_path, "r", errors="replace").read()
                        if len(content) > 5000:
                            content = content[:5000] + "\n[... truncated]"
                        summary["readme"] = content
                    except OSError:
                        pass
                    break

            # Read package manifest
            for config_name in ("package.json", "pyproject.toml", "Cargo.toml", "go.mod"):
                config_path = os.path.join(dep_dir, config_name)
                if os.path.isfile(config_path):
                    try:
                        content = open(config_path, "r", errors="replace").read()
                        if len(content) > 3000:
                            content = content[:3000] + "\n[... truncated]"
                        summary["manifest"] = content
                    except OSError:
                        pass
                    break

            if "readme" in summary or "manifest" in summary:
                summaries.append(summary)

        return summaries

    async def _run_analysis(
        self,
        project_name: str,
        project_description: str,
        file_tree: str,
        files: list[dict],
        dependencies: list[dict],
        dep_summaries: list[dict],
        provider,
    ) -> dict | None:
        """Send sampled codebase to LLM for structured analysis."""
        # Build file contents section
        files_text = ""
        for f in files:
            files_text += f"\n--- [{f['category']}] {f['path']} ---\n{f['content']}\n"

        # Build dependencies section
        deps_text = ""
        if dependencies:
            deps_text = "\n\nListed dependencies:\n"
            for dep in dependencies:
                deps_text += f"- {dep.get('name', '?')}"
                if dep.get("repo_url"):
                    deps_text += f" ({dep['repo_url']})"
                if dep.get("description"):
                    deps_text += f": {dep['description']}"
                deps_text += "\n"

        # Add cloned dependency docs
        if dep_summaries:
            deps_text += "\n\nDependency documentation:\n"
            for ds in dep_summaries:
                deps_text += f"\n--- {ds['name']} ---\n"
                if "readme" in ds:
                    deps_text += ds["readme"] + "\n"
                if "manifest" in ds:
                    deps_text += f"\nManifest:\n{ds['manifest']}\n"

        messages = [
            LLMMessage(role="system", content=ANALYZER_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"Project: {project_name}\n"
                    f"Description: {project_description or 'N/A'}\n\n"
                    f"Directory structure:\n{file_tree}\n\n"
                    f"Sampled files ({len(files)} files):\n"
                    f"{files_text}"
                    f"{deps_text}"
                ),
            ),
        ]

        try:
            response = await asyncio.wait_for(
                provider.send_message(messages, temperature=0.1),
                timeout=180,  # 3 minute timeout
            )
        except asyncio.TimeoutError:
            logger.warning("LLM analysis timed out for project")
            return None

        try:
            text = response.content.strip()
            # Strip markdown code fences
            if text.startswith("```"):
                lines = text.split("\n")
                start = 1
                end = len(lines) - 1
                if lines[end].strip() == "```":
                    text = "\n".join(lines[start:end])
                else:
                    text = "\n".join(lines[start:])

            # Extract JSON object
            brace_start = text.find("{")
            if brace_start >= 0:
                depth = 0
                for i in range(brace_start, len(text)):
                    if text[i] == "{":
                        depth += 1
                    elif text[i] == "}":
                        depth -= 1
                        if depth == 0:
                            text = text[brace_start:i + 1]
                            break

            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Failed to parse analysis JSON, storing raw response")
            return {"summary": response.content, "raw": True}

    async def _update_settings(self, project_id: str, updates: dict) -> None:
        """Merge keys into the project's settings_json."""
        current = await self.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", project_id
        )
        current_settings = current["settings_json"] if current else {}
        if isinstance(current_settings, str):
            current_settings = json.loads(current_settings)
        if not isinstance(current_settings, dict):
            current_settings = {}
        current_settings.update(updates)
        await self.db.execute(
            "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
            project_id,
            current_settings,
        )
