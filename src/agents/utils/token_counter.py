"""Model-aware token counting utility."""

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

# Model family to context window mapping
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 128_000,
    "claude-3-5-sonnet": 200_000,
    "claude-3-opus": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3-5-haiku": 200_000,
    "claude-sonnet-4-20250514": 200_000,
}

@lru_cache(maxsize=4)
def _get_tiktoken_encoding(encoding_name: str):
    """Cache tiktoken encoding instances (expensive to create)."""
    try:
        import tiktoken
        return tiktoken.get_encoding(encoding_name)
    except Exception:
        logger.debug("tiktoken encoding %s not available", encoding_name)
        return None


def _get_encoding_for_model(model: str) -> str:
    """Return tiktoken encoding name for a model."""
    if "o3" in model or "o1" in model:
        return "o200k_base"
    if "gpt" in model:
        return "cl100k_base"
    return "cl100k_base"  # fallback


def count_tokens(text: str, model: str = "default") -> int:
    """Count tokens in text for a given model.

    Uses tiktoken for OpenAI models, heuristic for others.
    """
    if not text:
        return 0

    # Try tiktoken for OpenAI models
    if any(prefix in model.lower() for prefix in ("gpt", "o3", "o1")):
        enc_name = _get_encoding_for_model(model)
        enc = _get_tiktoken_encoding(enc_name)
        if enc:
            return len(enc.encode(text))

    # Heuristic fallback: ~4 chars per token (conservative)
    return len(text) // 4


def get_context_window(model: str) -> int:
    """Get the context window size for a model."""
    # Exact match
    if model in MODEL_CONTEXT_WINDOWS:
        return MODEL_CONTEXT_WINDOWS[model]

    # Partial match
    model_lower = model.lower()
    for key, window in MODEL_CONTEXT_WINDOWS.items():
        if key in model_lower:
            return window

    # Default for unknown models (conservative)
    return 32_000
