from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    # Database
    database_url: str = "postgresql://agents:agents_dev@localhost:5432/agents"

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # JWT
    jwt_secret_key: str = "change-me-to-a-random-secret"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 1440

    # Encryption
    encryption_key: str = "change-me"

    # Orchestrator
    orchestrator_poll_interval: int = 5  # legacy — used by fallback poll
    orchestrator_max_concurrent: int = 50
    orchestrator_lock_ttl: int = 300
    orchestrator_health_check_interval: int = 60  # seconds between health checks

    # Default provider keys (optional)
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Workspaces
    workspace_root: str = "/tmp/agents-workspaces"

    # Admin seed — first user with this email gets promoted to admin on startup
    admin_seed_email: str = ""

    # Logging
    log_level: str = "INFO"


settings = Settings()
