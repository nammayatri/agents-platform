from pydantic import BaseModel


class ProviderConfigInput(BaseModel):
    provider_type: str  # 'anthropic' | 'openai' | 'self_hosted'
    display_name: str
    api_base_url: str | None = None
    api_key: str | None = None
    default_model: str
    fast_model: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.1
    extra_config: dict = {}


class ProviderConfigOut(BaseModel):
    id: str
    owner_id: str | None = None
    provider_type: str
    display_name: str
    api_base_url: str | None = None
    default_model: str
    fast_model: str | None = None
    max_tokens: int
    temperature: float
    is_active: bool


class NotificationChannelInput(BaseModel):
    channel_type: str  # 'slack' | 'email' | 'webhook'
    display_name: str
    config_json: dict  # {webhook_url, email, slack_token, channel_id, ...}
    notify_on: list[str] = ["stuck", "failed", "completed", "review"]


class NotificationChannelOut(BaseModel):
    id: str
    user_id: str
    channel_type: str
    display_name: str
    config_json: dict
    is_active: bool
    notify_on: list[str]
