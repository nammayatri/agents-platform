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
from agents.providers.registry import get_registry
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
configuration files, sampled source code, and cross-repo integration data, produce a \
structured understanding.

Your analysis should cover:
1. **Purpose**: What the project does, its core value proposition
2. **Architecture**: High-level architecture, key modules/components, data flow
3. **Tech stack**: Languages, frameworks, databases, infrastructure
4. **Key patterns**: Coding conventions, design patterns, error handling approach
5. **Dependency relationships**: How each listed dependency is used, including specific \
files and interfaces at integration points
6. **Cross-repo links**: How the main repo integrates with each dependency — shared types, \
API calls, imports, configuration references
7. **API surface**: Key endpoints, interfaces, or entry points
8. **Testing**: Testing approach, test frameworks, CI/CD patterns
9. **Build & compile workflow**: How to build the project from scratch. Include exact commands, \
any code generation steps (DSL transpilers, protobuf, codegen), required environment setup \
(nix, docker, virtualenv), and the order of operations. This is critical — agents will use this \
to build and verify their changes.
10. **Important context**: Anything an AI agent should know before working on tasks. Include \
gotchas, non-obvious conventions, and things that commonly trip up newcomers.

Output a JSON object with these keys:
- "purpose": string (2-3 sentences)
- "architecture": string (paragraph describing structure)
- "tech_stack": list of strings
- "key_patterns": list of strings (important conventions)
- "dependency_map": list of {"name": string, "role": string, "integration_files": list of strings}
- "cross_repo_links": list of {"dep_name": string, "main_repo_files": list of strings, \
"shared_interfaces": list of strings, "integration_pattern": string}
- "api_surface": string (key endpoints or interfaces)
- "testing_approach": string
- "build_workflow": string (step-by-step: how to build, compile, run code generation, etc.)
- "important_context": list of strings (things an agent must know)
- "summary": string (1 paragraph executive summary)
"""

DEP_ANALYZER_SYSTEM_PROMPT = """\
You are a dependency analyst. Given a dependency repository's directory structure, \
documentation, configuration, and sampled source code, produce a structured understanding \
focused on how a consumer would integrate with this project.

Your analysis should cover:
1. **Purpose**: What this dependency does, its core value proposition
2. **Architecture**: High-level structure, key modules/components
3. **Tech stack**: Languages, frameworks
4. **Key patterns**: Coding conventions, API design patterns
5. **API surface**: Key exports, public interfaces, endpoints, or entry points that \
consumers use — be specific about function signatures, class names, route paths
6. **Exports**: List of main exported modules, classes, functions, or types
7. **Important context**: Anything an AI agent should know when working with code \
that depends on this project

Output a JSON object with these keys:
- "purpose": string (2-3 sentences)
- "architecture": string (paragraph describing structure)
- "tech_stack": list of strings
- "key_patterns": list of strings (important conventions)
- "api_surface": string (detailed description of key public interfaces)
- "exports": list of strings (main exported modules/classes/functions)
- "important_context": list of strings (things an agent must know)
- "summary": string (1 paragraph executive summary)
"""

LINKING_SYSTEM_PROMPT = """\
You are a cross-repo integration analyst. Given the understanding of a main project and \
its dependency repos, plus cross-repo integration points found in the code, produce a \
document describing how all the repositories work together.

Your analysis should cover:
1. **Overview**: How the repos relate to each other at a high level
2. **Integrations**: Specific integration points between repos — API calls, shared \
packages, imports, configuration references, data flow
3. **Shared types**: Types, interfaces, or schemas shared across repos
4. **Ownership**: Which repo owns what responsibility

Output a JSON object with these keys:
- "overview": string (2-3 paragraphs describing how repos work together)
- "integrations": list of {"source_repo": string, "target_repo": string, \
"pattern": string, "shared_interfaces": list of strings, "data_flow": string}
- "shared_types": list of strings (shared type/interface names)
- "architecture_diagram_text": string (ASCII or text description of how repos connect)
"""


class ProjectAnalyzer:
    def __init__(self, db: asyncpg.Pool, redis=None):
        self.db = db
        self.redis = redis
        self.registry = get_registry(db)
        self.workspace_mgr = WorkspaceManager(db, settings.workspace_root)

    async def _publish_progress(self, project_id: str, step: str, detail: str) -> None:
        """Publish analysis progress via Redis pub/sub AND persist to DB.

        Persisting ensures the frontend can hydrate the current step on
        page refresh — Redis pub/sub is ephemeral and missed events are
        lost forever.
        """
        # Persist so frontend can hydrate on refresh (single lightweight UPDATE)
        await self.db.execute(
            "UPDATE projects SET settings_json = "
            "jsonb_set(jsonb_set(COALESCE(settings_json, '{}')::jsonb, "
            "'{analysis_step}', $2::jsonb), '{analysis_detail}', $3::jsonb), "
            "updated_at = NOW() WHERE id = $1",
            project_id,
            json.dumps(step),
            json.dumps(detail),
        )

        # Stream to connected WebSocket clients
        if self.redis:
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
        await self._publish_progress(project_id, "cloning", "Fetching latest code from all repos...")

        try:
            # Clone/pull the repo into workspace (deps cloned in parallel internally)
            project_dir = await self.workspace_mgr.setup_project_workspace(project_id)
            repo_dir = os.path.join(project_dir, "repo")

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
            deps_dir = os.path.join(project_dir, "deps")

            provider_task = asyncio.create_task(self._resolve_provider(project_id))

            sampled = self._smart_sample(repo_dir)
            dep_summaries = self._sample_dependencies(deps_dir, context_docs)

            # Find cross-repo integration points (main repo -> deps)
            dep_names = [d.get("name", "") for d in context_docs if d.get("name")]
            integration_points = self._find_integration_points(repo_dir, dep_names)

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
                dep_detail = f"Read docs for {len(dep_summaries)} dependencies"
                if integration_points:
                    total_refs = sum(len(v) for v in integration_points.values())
                    dep_detail += f", found {total_refs} cross-repo references"
                await self._publish_progress(
                    project_id, "dependencies", dep_detail,
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
                integration_points=integration_points or None,
            )

            if analysis:
                await self._update_settings(project_id, {
                    "analysis_status": "complete",
                    "project_understanding": analysis,
                })
                # Also write to workspace for agent reference
                analysis_path = os.path.join(project_dir, "analysis.json")
                with open(analysis_path, "w") as f:
                    json.dump(analysis, f, indent=2)

                logger.info(
                    "Project %s analyzed: %d files sampled from workspace",
                    project_id, len(sampled["files"]),
                )

                # Build code indexes (structural + embedding) at project level
                await self._publish_progress(project_id, "indexing", "Building code search indexes...")
                _INDEX_TIMEOUT = 180  # 3 minutes — skip if repo is too large
                main_indexed = False
                try:
                    from agents.indexing import build_indexes_and_repo_map

                    project_index_dir = os.path.join(project_dir, ".agent_index")
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(
                                build_indexes_and_repo_map,
                                repo_dir,
                                cache_dir=project_index_dir,
                            ),
                            timeout=_INDEX_TIMEOUT,
                        )
                        main_indexed = True
                        logger.info("Project %s: code indexes built at %s", project_id, project_index_dir)
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Project %s: code indexing timed out after %ds (repo too large)",
                            project_id, _INDEX_TIMEOUT,
                        )
                        await self._publish_progress(
                            project_id, "indexing",
                            "Code indexing skipped (repo too large). Agents will use file search instead.",
                        )
                except Exception:
                    logger.warning("Project %s: code indexing failed (non-fatal)", project_id, exc_info=True)

                # Per-dependency LLM analysis
                dep_understandings: dict[str, dict] = {}
                if context_docs and dep_summaries:
                    total_deps = len(dep_summaries)
                    for idx, ds in enumerate(dep_summaries, 1):
                        dep_name = ds["name"]
                        dep_dir_name = dep_name.replace("/", "_").replace(" ", "_")
                        dep_dir = os.path.join(deps_dir, dep_dir_name)
                        if not os.path.isdir(dep_dir):
                            continue
                        await self._publish_progress(
                            project_id, "dep_analysis",
                            f"Analyzing dependency: {dep_name} ({idx}/{total_deps})",
                        )
                        try:
                            dep_config = next(
                                (d for d in context_docs if d.get("name") == dep_name), {}
                            )
                            dep_u = await self._analyze_single_dependency(
                                dep_name, dep_dir, dep_config, provider,
                            )
                            if dep_u:
                                dep_understandings[dep_name] = dep_u
                        except Exception:
                            logger.warning(
                                "Project %s: dep analysis failed for %s (non-fatal)",
                                project_id, dep_name, exc_info=True,
                            )

                # Generate cross-repo linking document
                linking_document = None
                if dep_understandings:
                    await self._publish_progress(
                        project_id, "linking", "Building cross-repo linking document...",
                    )
                    try:
                        linking_document = await self._generate_linking_document(
                            project_name=project["name"],
                            main_understanding=analysis,
                            dep_understandings=dep_understandings,
                            integration_points=integration_points or {},
                            provider=provider,
                        )
                    except Exception:
                        logger.warning(
                            "Project %s: linking document generation failed (non-fatal)",
                            project_id, exc_info=True,
                        )
                    # If LLM-based linking failed, build a deterministic fallback
                    if not linking_document or linking_document.get("raw"):
                        linking_document = self._build_deterministic_linking(
                            project_name=project["name"],
                            dep_understandings=dep_understandings,
                            integration_points=integration_points or {},
                        )

                # Per-dependency code indexing
                index_metadata: dict = {
                    "main": {"indexed": main_indexed},
                    "deps": {},
                }
                if context_docs:
                    await self._publish_progress(
                        project_id, "dep_indexing", "Indexing dependency repos...",
                    )
                    for dep in context_docs:
                        dep_name = dep.get("name", "")
                        dep_dir_name = dep_name.replace("/", "_").replace(" ", "_")
                        dep_dir = os.path.join(deps_dir, dep_dir_name)
                        if not dep_dir_name or not os.path.isdir(dep_dir):
                            continue
                        await self._publish_progress(
                            project_id, "dep_indexing",
                            f"Indexing: {dep_name}",
                        )
                        try:
                            dep_index_dir = os.path.join(
                                project_dir, ".agent_index_deps", dep_dir_name,
                            )
                            try:
                                dep_repo_map = await asyncio.wait_for(
                                    asyncio.to_thread(
                                        build_indexes_and_repo_map,
                                        dep_dir,
                                        cache_dir=dep_index_dir,
                                    ),
                                    timeout=_INDEX_TIMEOUT,
                                )
                            except asyncio.TimeoutError:
                                logger.warning(
                                    "Project %s: dep indexing timed out for %s",
                                    project_id, dep_name,
                                )
                                dep_repo_map = None
                            logger.info(
                                "Project %s: dep index built for %s at %s",
                                project_id, dep_name, dep_index_dir,
                            )
                            index_metadata["deps"][dep_name] = {
                                "indexed": True,
                                "has_repo_map": bool(dep_repo_map),
                            }
                        except Exception:
                            logger.warning(
                                "Project %s: dep indexing failed for %s (non-fatal)",
                                project_id, dep_name, exc_info=True,
                            )

                # Store all new data
                extra_settings: dict = {}
                if dep_understandings:
                    extra_settings["dep_understandings"] = dep_understandings
                if linking_document:
                    extra_settings["linking_document"] = linking_document
                extra_settings["index_metadata"] = index_metadata
                if extra_settings:
                    await self._update_settings(project_id, extra_settings)

                # Write context files to workspace for agent self-serve reads
                try:
                    self.write_context_files(
                        workspace_path=project_dir,
                        project_name=project["name"],
                        understanding=analysis,
                        dep_understandings=dep_understandings or None,
                        linking_document=linking_document,
                    )
                    logger.info("Project %s: context files written to %s/.context/",
                                project_id, project_dir)
                except Exception:
                    logger.warning("Project %s: failed to write context files (non-fatal)",
                                   project_id, exc_info=True)

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
        """Read README, config, file tree, and key source files from dependency repos."""
        summaries: list[dict] = []
        if not os.path.isdir(deps_dir):
            return summaries

        dep_budget = 20_000  # max chars of source content per dependency

        for dep in context_docs:
            dep_name = dep.get("name", "").replace("/", "_").replace(" ", "_")
            dep_dir = os.path.join(deps_dir, dep_name)
            if not os.path.isdir(dep_dir):
                continue

            summary: dict = {"name": dep.get("name", dep_name)}

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

            # Build shallow file tree (max depth 3)
            tree_lines: list[str] = []
            for root, dirs, files in os.walk(dep_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                rel = os.path.relpath(root, dep_dir)
                depth = 0 if rel == "." else rel.count(os.sep) + 1
                if depth > 3:
                    continue
                indent = "  " * depth
                if rel != ".":
                    tree_lines.append(f"{indent}{os.path.basename(root)}/")
                for f in sorted(files):
                    ext = os.path.splitext(f)[1].lower()
                    if ext not in SKIP_EXTENSIONS:
                        tree_lines.append(f"{indent}  {f}")
            if tree_lines:
                summary["tree"] = "\n".join(tree_lines[:200])

            # Read key source files: entry points + shallow src/lib files
            key_files: list[dict] = []
            chars_used = 0
            candidates: list[str] = []

            for root, dirs, files in os.walk(dep_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                rel_root = os.path.relpath(root, dep_dir)
                depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
                if depth > 3:
                    continue
                for f in sorted(files):
                    basename = f.lower()
                    ext = os.path.splitext(f)[1].lower()
                    if ext in SKIP_EXTENSIONS:
                        continue
                    rel_path = os.path.join(rel_root, f) if rel_root != "." else f
                    # Prioritize entry points and source in key dirs
                    if basename in ENTRY_POINTS:
                        candidates.insert(0, rel_path)
                    elif ext in (".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".java"):
                        parts = rel_path.lower().split(os.sep)
                        if any(p in ("src", "lib", "app", "api", "core") for p in parts):
                            candidates.append(rel_path)

            for rel_path in candidates[:15]:
                if chars_used >= dep_budget:
                    break
                abs_path = os.path.join(dep_dir, rel_path)
                try:
                    content = open(abs_path, "r", errors="replace").read()
                    if len(content) > 8000:
                        content = content[:8000] + "\n[... truncated]"
                    if chars_used + len(content) > dep_budget:
                        continue
                    key_files.append({"path": rel_path, "content": content})
                    chars_used += len(content)
                except (OSError, UnicodeDecodeError):
                    pass

            if key_files:
                summary["key_files"] = key_files

            if any(k in summary for k in ("readme", "manifest", "key_files", "tree")):
                summaries.append(summary)

        return summaries

    def _find_integration_points(
        self, repo_dir: str, dep_names: list[str],
    ) -> dict[str, list[str]]:
        """Scan main repo for imports/references to each dependency name.

        Returns a mapping of dep_name -> list of "file:line: preview" strings.
        """
        import re

        results: dict[str, list[str]] = {}
        if not dep_names or not os.path.isdir(repo_dir):
            return results

        # Build search variants for each dep name
        dep_patterns: dict[str, re.Pattern] = {}
        for name in dep_names:
            variants = {name}
            # Hyphenated <-> underscored
            if "-" in name:
                variants.add(name.replace("-", "_"))
            if "_" in name:
                variants.add(name.replace("_", "-"))
            # Scoped package (@org/name -> name part)
            if "/" in name:
                variants.add(name.split("/")[-1])
            # Build a case-insensitive regex matching any variant
            escaped = [re.escape(v) for v in variants]
            dep_patterns[name] = re.compile("|".join(escaped), re.IGNORECASE)

        # Walk repo (max depth 5) and grep each file
        source_exts = {".py", ".ts", ".js", ".tsx", ".jsx", ".rs", ".go", ".java",
                       ".rb", ".cs", ".json", ".yaml", ".yml", ".toml", ".cfg"}

        for root, dirs, files in os.walk(repo_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
            rel_root = os.path.relpath(root, repo_dir)
            depth = 0 if rel_root == "." else rel_root.count(os.sep) + 1
            if depth > 5:
                continue

            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in source_exts:
                    continue
                rel_path = os.path.join(rel_root, fname) if rel_root != "." else fname
                abs_path = os.path.join(root, fname)

                try:
                    with open(abs_path, "r", errors="replace") as fh:
                        for line_no, line in enumerate(fh, 1):
                            for dep_name, pattern in dep_patterns.items():
                                if pattern.search(line):
                                    hits = results.setdefault(dep_name, [])
                                    if len(hits) < 15:
                                        preview = line.strip()[:120]
                                        hits.append(f"{rel_path}:{line_no}: {preview}")
                except (OSError, UnicodeDecodeError):
                    pass

        return results

    async def _run_analysis(
        self,
        project_name: str,
        project_description: str,
        file_tree: str,
        files: list[dict],
        dependencies: list[dict],
        dep_summaries: list[dict],
        provider,
        integration_points: dict[str, list[str]] | None = None,
    ) -> dict | None:
        """Send sampled codebase to LLM for structured analysis."""
        # Build file contents section
        files_text = ""
        for f in files:
            files_text += f"\n--- [{f['category']}] {f['path']} ---\n{f['content']}\n"

        # Build dependencies section — include user-provided descriptions prominently
        deps_text = ""
        if dependencies:
            deps_text = "\n\nProject dependencies (configured by the project owner):\n"
            for dep in dependencies:
                name = dep.get("name", "?")
                deps_text += f"\n### {name}\n"
                if dep.get("description"):
                    deps_text += f"Description: {dep['description']}\n"
                if dep.get("repo_url"):
                    deps_text += f"Repository: {dep['repo_url']}\n"

        # Add cloned dependency docs, key files, and tree
        if dep_summaries:
            deps_text += "\n\nDependency documentation:\n"
            for ds in dep_summaries:
                deps_text += f"\n--- {ds['name']} ---\n"
                if "readme" in ds:
                    deps_text += ds["readme"] + "\n"
                if "manifest" in ds:
                    deps_text += f"\nManifest:\n{ds['manifest']}\n"
                if "tree" in ds:
                    deps_text += f"\nFile tree:\n{ds['tree']}\n"
                if "key_files" in ds:
                    deps_text += f"\nKey source files ({len(ds['key_files'])}):\n"
                    for kf in ds["key_files"]:
                        deps_text += f"\n  -- {kf['path']} --\n{kf['content']}\n"

        # Add integration points (cross-repo references)
        if integration_points:
            deps_text += "\n\nCross-repo integration points (main repo references to dependencies):\n"
            for dep_name, hits in integration_points.items():
                if hits:
                    deps_text += f"\n{dep_name} ({len(hits)} references):\n"
                    for hit in hits:
                        deps_text += f"  {hit}\n"

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

        return self._parse_json_response(response.content)

    async def _analyze_single_dependency(
        self, dep_name: str, dep_dir: str, dep_config: dict, provider,
    ) -> dict | None:
        """Run a full LLM analysis on a single dependency repo."""
        sampled = self._smart_sample(dep_dir)
        if not sampled["files"]:
            return None

        files_text = ""
        for f in sampled["files"]:
            files_text += f"\n--- [{f['category']}] {f['path']} ---\n{f['content']}\n"

        desc = dep_config.get("description", "")
        messages = [
            LLMMessage(role="system", content=DEP_ANALYZER_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"Dependency: {dep_name}\n"
                    f"Description: {desc or 'N/A'}\n"
                    f"Repo URL: {dep_config.get('repo_url', 'N/A')}\n\n"
                    f"Directory structure:\n{sampled['tree']}\n\n"
                    f"Sampled files ({len(sampled['files'])} files):\n"
                    f"{files_text}"
                ),
            ),
        ]

        try:
            response = await asyncio.wait_for(
                provider.send_message(messages, temperature=0.1),
                timeout=120,
            )
        except asyncio.TimeoutError:
            logger.warning("Dep analysis timed out for %s", dep_name)
            return None

        return self._parse_json_response(response.content)

    async def _generate_linking_document(
        self,
        project_name: str,
        main_understanding: dict,
        dep_understandings: dict[str, dict],
        integration_points: dict[str, list[str]],
        provider,
    ) -> dict | None:
        """Generate a cross-repo linking document describing how repos work together."""
        # Build context from understandings
        main_summary = main_understanding.get("summary", "")
        main_purpose = main_understanding.get("purpose", "")

        deps_context = ""
        for dep_name, dep_u in dep_understandings.items():
            deps_context += f"\n--- {dep_name} ---\n"
            deps_context += f"Purpose: {dep_u.get('purpose', 'N/A')}\n"
            deps_context += f"API surface: {dep_u.get('api_surface', 'N/A')}\n"
            exports = dep_u.get("exports", [])
            if exports:
                deps_context += f"Exports: {', '.join(exports[:20])}\n"
            deps_context += f"Summary: {dep_u.get('summary', 'N/A')}\n"

        integration_text = ""
        if integration_points:
            integration_text = "\n\nCross-repo references found in main repo:\n"
            for dep_name, hits in integration_points.items():
                if hits:
                    integration_text += f"\n{dep_name} ({len(hits)} references):\n"
                    for hit in hits[:10]:
                        integration_text += f"  {hit}\n"

        messages = [
            LLMMessage(role="system", content=LINKING_SYSTEM_PROMPT),
            LLMMessage(
                role="user",
                content=(
                    f"Main project: {project_name}\n"
                    f"Purpose: {main_purpose}\n"
                    f"Summary: {main_summary}\n\n"
                    f"Dependency repos:\n{deps_context}"
                    f"{integration_text}"
                ),
            ),
        ]

        try:
            response = await asyncio.wait_for(
                provider.send_message(messages, temperature=0.1),
                timeout=180,
            )
        except asyncio.TimeoutError:
            logger.warning("Linking document generation timed out")
            return None

        return self._parse_json_response(response.content)

    @staticmethod
    def _build_deterministic_linking(
        project_name: str,
        dep_understandings: dict[str, dict],
        integration_points: dict[str, list[str]],
    ) -> dict:
        """Build a deterministic linking document from available data (no LLM)."""
        dep_names = list(dep_understandings.keys())
        overview_parts = [
            f"{project_name} integrates with {len(dep_names)} "
            f"dependency repo{'s' if len(dep_names) != 1 else ''}: "
            f"{', '.join(dep_names)}."
        ]
        for dn, du in dep_understandings.items():
            purpose = du.get("purpose", "")
            if purpose:
                overview_parts.append(f"  - {dn}: {purpose}")

        integrations = []
        for dep_name, hits in integration_points.items():
            if hits:
                files = list({h.split(":")[0] for h in hits[:10]})
                integrations.append({
                    "source_repo": project_name,
                    "target_repo": dep_name,
                    "pattern": f"Referenced in {len(hits)} location(s)",
                    "shared_interfaces": files[:5],
                    "data_flow": "",
                })

        return {
            "overview": "\n".join(overview_parts),
            "integrations": integrations,
            "shared_types": [],
        }

    def _parse_json_response(self, content: str) -> dict | None:
        """Parse a JSON response from LLM, handling markdown fences."""
        try:
            text = content.strip()
            # Strip markdown code fences
            if text.startswith("```"):
                lines = text.split("\n")
                start = 1
                end = len(lines) - 1
                if lines[end].strip() == "```":
                    text = "\n".join(lines[start:end])
                else:
                    text = "\n".join(lines[start:])

            # Try direct parse first (handles clean JSON)
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                pass

            # Extract JSON using string-aware brace matching
            brace_start = text.find("{")
            if brace_start >= 0:
                depth = 0
                in_string = False
                escape_next = False
                for i in range(brace_start, len(text)):
                    c = text[i]
                    if escape_next:
                        escape_next = False
                        continue
                    if c == "\\":
                        escape_next = True
                        continue
                    if c == '"' and not escape_next:
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if c == "{":
                        depth += 1
                    elif c == "}":
                        depth -= 1
                        if depth == 0:
                            candidate = text[brace_start:i + 1]
                            try:
                                return json.loads(candidate)
                            except (json.JSONDecodeError, ValueError):
                                break

            # Last resort: find any JSON object in the text
            import re
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                try:
                    return json.loads(json_match.group())
                except (json.JSONDecodeError, ValueError):
                    pass

            logger.warning("Failed to parse JSON response, storing raw")
            return {"summary": content, "raw": True}
        except Exception:
            logger.warning("Failed to parse JSON response", exc_info=True)
            return {"summary": content, "raw": True}

    async def _update_settings(self, project_id: str, updates: dict) -> None:
        """Merge keys into the project's settings_json.

        Transparently handles both old and new formats. Analysis-related keys
        are mapped to the new ``understanding`` namespace when the settings
        are already in new format.

        When analysis_status is set to a terminal value (complete/failed/no_docs),
        automatically clears the ephemeral analysis_step/analysis_detail fields
        so the frontend doesn't show stale progress on next load.
        """
        from agents.utils.settings_helpers import is_new_format, parse_settings

        current = await self.db.fetchrow(
            "SELECT settings_json FROM projects WHERE id = $1", project_id
        )
        current_settings = parse_settings(current["settings_json"] if current else None)

        if is_new_format(current_settings):
            # Map flat analysis keys into the understanding namespace
            understanding = current_settings.setdefault("understanding", {})
            key_map = {
                "analysis_status": "status",
                "project_understanding": "project",
                "dep_understandings": "dependencies",
                "linking_document": "linking",
            }
            for old_key, new_key in key_map.items():
                if old_key in updates:
                    understanding[new_key] = updates.pop(old_key)
            # Any remaining keys (index_metadata, etc.) go at top level
            current_settings.update(updates)
        else:
            # Old format — just merge flat
            current_settings.update(updates)

        # Clean up progress fields on terminal status
        terminal_status = updates.get("analysis_status")
        # Also check if we mapped it into understanding.status
        if terminal_status is None and is_new_format(current_settings):
            terminal_status = current_settings.get("understanding", {}).get("status")
        if terminal_status in ("complete", "failed", "no_docs", None):
            current_settings.pop("analysis_step", None)
            current_settings.pop("analysis_detail", None)

        await self.db.execute(
            "UPDATE projects SET settings_json = $2, updated_at = NOW() WHERE id = $1",
            project_id,
            current_settings,
        )

    # ---- Context files (written to workspace for agent self-serve) ----

    @staticmethod
    def write_context_files(
        workspace_path: str,
        project_name: str,
        understanding: dict | None,
        dep_understandings: dict[str, dict] | None,
        linking_document: dict | None,
    ) -> None:
        """Write understanding data as readable markdown files in .context/.

        These files let agents self-serve project context via read_file()
        instead of having it injected into every system prompt.
        """
        context_dir = os.path.join(workspace_path, ".context")
        os.makedirs(context_dir, exist_ok=True)

        # Main repo understanding
        if understanding and isinstance(understanding, dict):
            md = _format_understanding_md(project_name, understanding)
            with open(os.path.join(context_dir, "UNDERSTANDING.md"), "w") as f:
                f.write(md)

        # Per-dep understandings
        if dep_understandings and isinstance(dep_understandings, dict):
            deps_dir = os.path.join(context_dir, "deps")
            os.makedirs(deps_dir, exist_ok=True)
            for dep_name, dep_u in dep_understandings.items():
                if not isinstance(dep_u, dict):
                    continue
                safe_name = dep_name.replace("/", "_").replace(" ", "_")
                md = _format_dep_understanding_md(dep_name, dep_u)
                with open(os.path.join(deps_dir, f"{safe_name}.md"), "w") as f:
                    f.write(md)

        # Linking document
        if linking_document and isinstance(linking_document, dict):
            md = _format_linking_md(project_name, linking_document)
            with open(os.path.join(context_dir, "LINKING.md"), "w") as f:
                f.write(md)


# ---- Markdown formatting helpers (module-level) ----

def _format_understanding_md(project_name: str, u: dict) -> str:
    """Format a project understanding dict as readable markdown."""
    lines = [f"# {project_name}\n"]

    summary = u.get("summary", "")
    if summary:
        lines.append(f"## Summary\n{summary}\n")

    tech = u.get("tech_stack")
    if tech:
        if isinstance(tech, list):
            lines.append("## Tech Stack\n" + "\n".join(f"- {t}" for t in tech) + "\n")
        elif isinstance(tech, str):
            lines.append(f"## Tech Stack\n{tech}\n")

    arch = u.get("architecture", "")
    if arch:
        lines.append(f"## Architecture\n{arch}\n")

    patterns = u.get("key_patterns")
    if patterns:
        if isinstance(patterns, list):
            lines.append("## Key Patterns\n" + "\n".join(f"- {p}" for p in patterns) + "\n")
        elif isinstance(patterns, str):
            lines.append(f"## Key Patterns\n{patterns}\n")

    deps_map = u.get("dependency_map")
    if deps_map and isinstance(deps_map, list):
        lines.append("## Dependencies\n")
        for d in deps_map:
            if isinstance(d, dict):
                name = d.get("name", "?")
                role = d.get("role", "")
                lines.append(f"- **{name}**: {role}")
        lines.append("")

    cross_links = u.get("cross_repo_links")
    if cross_links and isinstance(cross_links, list):
        lines.append("## Cross-Repo Integration Points\n")
        for link in cross_links:
            if isinstance(link, dict):
                dep = link.get("dep_name", "?")
                pattern = link.get("integration_pattern", "")
                lines.append(f"- **{dep}**: {pattern}")
                main_files = link.get("main_repo_files", [])
                if main_files:
                    lines.append(f"  Files: {', '.join(main_files[:5])}")
        lines.append("")

    important = u.get("important_context")
    if important:
        if isinstance(important, list):
            lines.append("## Important Context\n" + "\n".join(f"- {c}" for c in important) + "\n")
        elif isinstance(important, str):
            lines.append(f"## Important Context\n{important}\n")

    return "\n".join(lines)


def _format_dep_understanding_md(dep_name: str, u: dict) -> str:
    """Format a dependency understanding dict as readable markdown."""
    lines = [f"# {dep_name}\n"]

    purpose = u.get("purpose", "")
    if purpose:
        lines.append(f"## Purpose\n{purpose}\n")

    summary = u.get("summary", "")
    if summary and summary != purpose:
        lines.append(f"## Summary\n{summary}\n")

    tech = u.get("tech_stack")
    if tech:
        if isinstance(tech, list):
            lines.append("## Tech Stack\n" + "\n".join(f"- {t}" for t in tech) + "\n")
        elif isinstance(tech, str):
            lines.append(f"## Tech Stack\n{tech}\n")

    arch = u.get("architecture", "")
    if arch:
        lines.append(f"## Architecture\n{arch}\n")

    api = u.get("api_surface", "")
    if api:
        lines.append(f"## API Surface\n{api}\n")

    exports = u.get("exports")
    if exports and isinstance(exports, list):
        lines.append("## Key Exports\n" + "\n".join(f"- `{e}`" for e in exports) + "\n")

    patterns = u.get("key_patterns")
    if patterns:
        if isinstance(patterns, list):
            lines.append("## Key Patterns\n" + "\n".join(f"- {p}" for p in patterns) + "\n")
        elif isinstance(patterns, str):
            lines.append(f"## Key Patterns\n{patterns}\n")

    important = u.get("important_context")
    if important:
        if isinstance(important, list):
            lines.append("## Important Context\n" + "\n".join(f"- {c}" for c in important) + "\n")
        elif isinstance(important, str):
            lines.append(f"## Important Context\n{important}\n")

    return "\n".join(lines)


def _format_linking_md(project_name: str, ld: dict) -> str:
    """Format a linking document dict as readable markdown."""
    lines = [f"# Cross-Repo Architecture: {project_name}\n"]

    overview = ld.get("overview", "")
    if overview:
        lines.append(f"## Overview\n{overview}\n")

    integrations = ld.get("integrations")
    if integrations and isinstance(integrations, list):
        lines.append("## Integrations\n")
        for intg in integrations:
            if not isinstance(intg, dict):
                continue
            src = intg.get("source_repo", "?")
            tgt = intg.get("target_repo", "?")
            pattern = intg.get("pattern", "")
            lines.append(f"### {src} -> {tgt}")
            if pattern:
                lines.append(f"**Pattern:** {pattern}")
            shared = intg.get("shared_interfaces", [])
            if shared:
                lines.append(f"**Shared interfaces:** {', '.join(shared)}")
            data_flow = intg.get("data_flow", "")
            if data_flow:
                lines.append(f"**Data flow:** {data_flow}")
            lines.append("")

    shared_types = ld.get("shared_types")
    if shared_types and isinstance(shared_types, list):
        lines.append("## Shared Types\n" + "\n".join(f"- `{t}`" for t in shared_types) + "\n")

    diagram = ld.get("architecture_diagram_text", "")
    if diagram:
        lines.append(f"## Architecture\n```\n{diagram}\n```\n")

    return "\n".join(lines)
