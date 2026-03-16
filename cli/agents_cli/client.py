"""HTTP client wrapper with auth, error handling."""

import functools
import sys

import click
import httpx
from rich.console import Console

from agents_cli import config

console = Console()


class APIError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"HTTP {status_code}: {detail}")


def _build_client(timeout: float = 30.0) -> httpx.Client:
    headers = {"Content-Type": "application/json"}
    token = config.get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return httpx.Client(
        base_url=config.get_api_url(),
        headers=headers,
        timeout=timeout,
    )


def _handle_response(response: httpx.Response) -> dict | list | None:
    if response.status_code == 401:
        config.clear_token()
        console.print("[red]Session expired. Please run 'agents login' again.[/red]")
        sys.exit(1)

    if response.status_code == 204:
        return None

    if response.status_code >= 400:
        try:
            body = response.json()
            detail = body.get("detail", f"HTTP {response.status_code}")
        except Exception:
            detail = response.text or f"HTTP {response.status_code}"
        raise APIError(response.status_code, detail)

    return response.json()


def handle_errors(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except APIError as e:
            console.print(f"[red]Error: {e.detail}[/red]")
            raise SystemExit(1)
        except httpx.ConnectError:
            url = config.get_api_url()
            console.print(f"[red]Cannot connect to server at {url}[/red]")
            console.print(
                "[dim]Check that the server is running, or update URL with: agents config set-url <url>[/dim]"
            )
            raise SystemExit(1)
        except httpx.TimeoutException:
            console.print("[red]Request timed out.[/red]")
            raise SystemExit(1)

    return wrapper


def require_auth():
    token = config.get_token()
    if not token:
        console.print("[red]Not logged in. Run 'agents login' first.[/red]")
        sys.exit(1)


def get(path: str, params: dict = None):
    with _build_client() as c:
        resp = c.get(f"/api{path}", params=params)
        return _handle_response(resp)


def post(path: str, json_data: dict = None, timeout: float = 30.0):
    with _build_client(timeout=timeout) as c:
        resp = c.post(f"/api{path}", json=json_data or {})
        return _handle_response(resp)


def put(path: str, json_data: dict = None):
    with _build_client() as c:
        resp = c.put(f"/api{path}", json=json_data or {})
        return _handle_response(resp)


def delete(path: str):
    with _build_client() as c:
        resp = c.delete(f"/api{path}")
        return _handle_response(resp)


def login(email: str, password: str) -> dict:
    with httpx.Client(base_url=config.get_api_url(), timeout=15.0) as c:
        resp = c.post(
            "/api/auth/login", json={"email": email, "password": password}
        )
        return _handle_response(resp)


def get_me() -> dict:
    return get("/auth/me")


def _is_full_uuid(value: str) -> bool:
    """Check if a string looks like a full UUID (contains dashes, 36 chars)."""
    return len(value) == 36 and value.count("-") == 4


def resolve_project_id(prefix: str) -> str:
    """Resolve a short project ID prefix to a full UUID."""
    if _is_full_uuid(prefix):
        return prefix
    projects = get("/projects")
    matches = [p for p in projects if p["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) == 0:
        raise APIError(404, f"No project found matching '{prefix}'")
    names = ", ".join(f"{m['id'][:8]} ({m['name']})" for m in matches)
    raise APIError(400, f"Ambiguous prefix '{prefix}' matches: {names}")


def resolve_todo_id(prefix: str, project_id: str | None = None) -> str:
    """Resolve a short todo ID prefix to a full UUID."""
    if _is_full_uuid(prefix):
        return prefix
    if project_id:
        todos = get(f"/projects/{project_id}/todos")
    else:
        # Try direct lookup -- if it fails, we can't resolve
        try:
            todo = get(f"/todos/{prefix}")
            return todo["id"]
        except APIError:
            raise APIError(404, f"No task found matching '{prefix}'. Provide a full UUID or use within a project context.")
    matches = [t for t in todos if t["id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]["id"]
    if len(matches) == 0:
        raise APIError(404, f"No task found matching '{prefix}'")
    titles = ", ".join(f"{m['id'][:8]} ({m['title']})" for m in matches)
    raise APIError(400, f"Ambiguous prefix '{prefix}' matches: {titles}")
