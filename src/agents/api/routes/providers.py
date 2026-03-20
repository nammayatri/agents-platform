from fastapi import APIRouter, HTTPException, status

from agents.api.deps import DB, CurrentUser
from agents.infra.crypto import encrypt
from agents.schemas.provider import ProviderConfigInput

router = APIRouter()


# --- AI Provider Config ---


@router.get("")
async def list_providers(user: CurrentUser, db: DB):
    rows = await db.fetch(
        """
        SELECT id, owner_id, provider_type, display_name, api_base_url,
               default_model, fast_model, max_tokens, temperature, is_active
        FROM ai_provider_configs
        WHERE owner_id = $1 OR owner_id IS NULL
        ORDER BY created_at DESC
        """,
        user["id"],
    )
    return [dict(r) for r in rows]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_provider(body: ProviderConfigInput, user: CurrentUser, db: DB):
    api_key_enc = encrypt(body.api_key) if body.api_key else None
    import json

    row = await db.fetchrow(
        """
        INSERT INTO ai_provider_configs (
            owner_id, provider_type, display_name, api_base_url,
            api_key_enc, default_model, fast_model, max_tokens,
            temperature, extra_config
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
        RETURNING id, owner_id, provider_type, display_name, api_base_url,
                  default_model, fast_model, max_tokens, temperature, is_active
        """,
        user["id"],
        body.provider_type,
        body.display_name,
        body.api_base_url,
        api_key_enc,
        body.default_model,
        body.fast_model,
        body.max_tokens,
        body.temperature,
        body.extra_config,
    )
    return dict(row)


@router.put("/{provider_id}")
async def update_provider(provider_id: str, body: ProviderConfigInput, user: CurrentUser, db: DB):
    existing = await db.fetchrow(
        "SELECT owner_id FROM ai_provider_configs WHERE id = $1", provider_id
    )
    if not existing:
        raise HTTPException(status_code=404)
    if existing["owner_id"] and str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403)

    api_key_enc = encrypt(body.api_key) if body.api_key else None
    import json

    row = await db.fetchrow(
        """
        UPDATE ai_provider_configs
        SET provider_type = $2, display_name = $3, api_base_url = $4,
            api_key_enc = COALESCE($5, api_key_enc), default_model = $6,
            fast_model = $7, max_tokens = $8, temperature = $9,
            extra_config = $10
        WHERE id = $1
        RETURNING id, owner_id, provider_type, display_name, api_base_url,
                  default_model, fast_model, max_tokens, temperature, is_active
        """,
        provider_id,
        body.provider_type,
        body.display_name,
        body.api_base_url,
        api_key_enc,
        body.default_model,
        body.fast_model,
        body.max_tokens,
        body.temperature,
        body.extra_config,
    )
    return dict(row)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_provider(provider_id: str, user: CurrentUser, db: DB):
    existing = await db.fetchrow(
        "SELECT owner_id FROM ai_provider_configs WHERE id = $1", provider_id
    )
    if not existing:
        raise HTTPException(status_code=404)
    if existing["owner_id"] and str(existing["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403)
    await db.execute("DELETE FROM ai_provider_configs WHERE id = $1", provider_id)


@router.post("/{provider_id}/test")
async def test_provider(provider_id: str, user: CurrentUser, db: DB):
    from agents.providers.registry import ProviderRegistry

    registry = ProviderRegistry(db)
    try:
        provider = await registry.instantiate(provider_id)
    except Exception as e:
        return {"status": "error", "detail": f"Configuration error: {e}"}

    try:
        healthy = await provider.health_check_detailed()
        return {"status": "ok" if healthy else "failed", "provider_id": provider_id}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


@router.get("/{provider_id}/models")
async def list_provider_models(provider_id: str, user: CurrentUser, db: DB):
    """List available models for a provider."""
    from agents.providers.registry import ProviderRegistry

    config = await db.fetchrow(
        "SELECT id, owner_id FROM ai_provider_configs WHERE id = $1", provider_id
    )
    if not config:
        raise HTTPException(status_code=404)
    if config["owner_id"] and str(config["owner_id"]) != str(user["id"]):
        raise HTTPException(status_code=403)

    registry = ProviderRegistry(db)
    try:
        provider = await registry.instantiate(provider_id)
        models = await provider.list_models()
        return {"provider_id": provider_id, "models": models}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list models: {e}")
