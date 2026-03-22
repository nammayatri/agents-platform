from pydantic import BaseModel


class ProviderConfigInput(BaseModel):
    provider_type: str  # 'anthropic' | 'openai' | 'self_hosted' | 'claude_code'
    display_name: str
    api_base_url: str | None = None
    api_key: str | None = None
    default_model: str
    fast_model: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.1
    extra_config: dict = {}

