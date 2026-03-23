"""Provider registry: resolves the correct AI provider for a given TODO item.

Resolution order:
1. TODO-level override (todo_items.ai_provider_id)
2. Project-level default (projects.ai_provider_id)
3. User-level default (users.settings_json.default_provider_id)
4. User-owned provider (any active provider owned by the task creator)
5. System-level default (first active global provider with no owner)
"""

import time

import asyncpg

from agents.infra.crypto import decrypt
from agents.providers.anthropic import AnthropicProvider
from agents.providers.base import AIProvider
from agents.providers.claude_code import ClaudeCodeProvider
from agents.providers.openai_provider import OpenAIProvider
from agents.providers.self_hosted import SelfHostedProvider

CACHE_TTL = 300  # seconds


class ProviderRegistry:
    def __init__(self, db: asyncpg.Pool):
        self.db = db
        self._cache: dict[str, tuple[AIProvider, float]] = {}

    async def resolve_for_todo(self, todo_id: str) -> AIProvider:
        """Resolve the AI provider for a specific TODO item."""
        todo = await self.db.fetchrow(
            "SELECT ai_provider_id, project_id, creator_id FROM todo_items WHERE id = $1",
            todo_id,
        )
        if not todo:
            raise ValueError(f"TODO {todo_id} not found")

        provider_id = todo["ai_provider_id"]

        if not provider_id:
            project = await self.db.fetchrow(
                "SELECT ai_provider_id, owner_id FROM projects WHERE id = $1",
                todo["project_id"],
            )
            provider_id = project["ai_provider_id"] if project else None

        if not provider_id:
            # User default
            user = await self.db.fetchrow(
                "SELECT settings_json FROM users WHERE id = $1",
                todo["creator_id"],
            )
            if user and user["settings_json"]:
                import json

                user_settings = (
                    json.loads(user["settings_json"])
                    if isinstance(user["settings_json"], str)
                    else user["settings_json"]
                )
                provider_id = user_settings.get("default_provider_id")

        if not provider_id:
            # User-owned provider (any active provider owned by the task creator)
            row = await self.db.fetchrow(
                "SELECT id FROM ai_provider_configs WHERE owner_id = $1 AND is_active = TRUE "
                "ORDER BY created_at ASC LIMIT 1",
                todo["creator_id"],
            )
            provider_id = str(row["id"]) if row else None

        if not provider_id:
            # System default (global provider with no owner)
            row = await self.db.fetchrow(
                "SELECT id FROM ai_provider_configs WHERE owner_id IS NULL AND is_active = TRUE "
                "ORDER BY created_at ASC LIMIT 1"
            )
            provider_id = str(row["id"]) if row else None

        if not provider_id:
            raise ValueError("No AI provider configured. Add one via Settings.")

        return await self.instantiate(provider_id)

    async def resolve_for_project(self, project_id: str, user_id: str) -> AIProvider:
        """Resolve the AI provider for a project-level chat (no TODO context)."""
        project = await self.db.fetchrow(
            "SELECT ai_provider_id, owner_id FROM projects WHERE id = $1",
            project_id,
        )
        provider_id = project["ai_provider_id"] if project else None

        if not provider_id:
            # User-owned provider
            row = await self.db.fetchrow(
                "SELECT id FROM ai_provider_configs WHERE owner_id = $1 AND is_active = TRUE "
                "ORDER BY created_at ASC LIMIT 1",
                user_id,
            )
            provider_id = str(row["id"]) if row else None

        if not provider_id:
            # System default
            row = await self.db.fetchrow(
                "SELECT id FROM ai_provider_configs WHERE owner_id IS NULL AND is_active = TRUE "
                "ORDER BY created_at ASC LIMIT 1"
            )
            provider_id = str(row["id"]) if row else None

        if not provider_id:
            raise ValueError("No AI provider configured. Add one via Settings.")

        return await self.instantiate(provider_id)

    async def instantiate(self, provider_id: str) -> AIProvider:
        """Create or return cached provider instance (TTL-based eviction)."""
        if provider_id in self._cache:
            provider, cached_at = self._cache[provider_id]
            if time.monotonic() - cached_at < CACHE_TTL:
                return provider

        config = await self.db.fetchrow(
            "SELECT * FROM ai_provider_configs WHERE id = $1", provider_id
        )
        if not config:
            raise ValueError(f"Provider config {provider_id} not found")

        api_key = decrypt(config["api_key_enc"]) if config["api_key_enc"] else None

        match config["provider_type"]:
            case "anthropic":
                if not api_key:
                    raise ValueError("Anthropic provider requires an API key")
                provider = AnthropicProvider(
                    api_key=api_key,
                    default_model=config["default_model"],
                    fast_model=config["fast_model"],
                )
            case "openai":
                if not api_key:
                    raise ValueError("OpenAI provider requires an API key")
                provider = OpenAIProvider(
                    api_key=api_key,
                    default_model=config["default_model"],
                    fast_model=config["fast_model"],
                )
            case "self_hosted":
                if not config["api_base_url"]:
                    raise ValueError("Self-hosted provider requires api_base_url")
                provider = SelfHostedProvider(
                    api_base_url=config["api_base_url"],
                    default_model=config["default_model"],
                    api_key=api_key,
                    fast_model=config["fast_model"],
                )
            case "claude_code":
                if not api_key:
                    raise ValueError("Claude Code provider requires an OAuth token")
                provider = ClaudeCodeProvider(
                    auth_token=api_key,
                    default_model=config["default_model"],
                    fast_model=config["fast_model"],
                )
            case _:
                raise ValueError(f"Unknown provider type: {config['provider_type']}")

        self._cache[provider_id] = (provider, time.monotonic())
        return provider


# ── Singleton accessor ──────────────────────────────────────────────
# Reuse a single ProviderRegistry per db pool to share the provider
# cache (and its HTTP clients) across requests instead of creating
# a fresh registry per API call.

_registry: ProviderRegistry | None = None


def get_registry(db: asyncpg.Pool) -> ProviderRegistry:
    """Return a shared ProviderRegistry, creating one if needed."""
    global _registry
    if _registry is None or _registry.db is not db:
        _registry = ProviderRegistry(db)
    return _registry
