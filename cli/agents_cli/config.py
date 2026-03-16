"""Config file management for ~/.agents-cli/config.json."""

import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".agents-cli"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "api_url": "http://localhost:8000",
    "token": None,
    "user_email": None,
    "user_display_name": None,
}


def _ensure_dir():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load() -> dict:
    if not CONFIG_FILE.exists():
        return dict(DEFAULT_CONFIG)
    try:
        return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text())}
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save(config: dict):
    _ensure_dir()
    CONFIG_FILE.write_text(json.dumps(config, indent=2) + "\n")


def get_api_url() -> str:
    return load()["api_url"]


def get_token() -> str | None:
    return load().get("token")


def set_token(token: str, email: str = None, display_name: str = None):
    cfg = load()
    cfg["token"] = token
    if email:
        cfg["user_email"] = email
    if display_name:
        cfg["user_display_name"] = display_name
    save(cfg)


def clear_token():
    cfg = load()
    cfg["token"] = None
    cfg["user_email"] = None
    cfg["user_display_name"] = None
    save(cfg)


def set_api_url(url: str):
    cfg = load()
    cfg["api_url"] = url.rstrip("/")
    save(cfg)
