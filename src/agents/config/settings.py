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

    # CORS
    cors_origins: str = "http://localhost:5173"  # comma-separated

    # Indexing & context management
    repo_map_token_budget: int = 4000
    context_compaction_keep_recent: int = 3

    # Logging
    log_level: str = "INFO"

    # Task pod mode (set on worker pods)
    task_pod_mode: bool = False
    task_todo_id: str = ""

    # K8s spawner (control plane settings for backend-0)
    k8s_namespace: str = "atlas-ai"
    k8s_worker_image: str = ""  # empty = use own image
    k8s_worker_service_account: str = "default"
    k8s_storage_class: str = ""
    k8s_node_selector: str = ""
    k8s_spawn_pods: bool = True  # set False to run tasks locally (dev mode)


settings = Settings()
