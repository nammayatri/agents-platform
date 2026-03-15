"""Factory for creating git provider instances, parsing repo URLs, and building clone URLs."""

import re
from urllib.parse import urlparse

from .base import GitProvider
from .bitbucket import BitbucketProvider
from .github import GitHubProvider
from .gitlab import GitLabProvider

DEFAULT_API_URLS: dict[str, str] = {
    "github": "https://api.github.com",
    "gitlab": "https://gitlab.com",
    "bitbucket": "https://api.bitbucket.org",
}

HOST_TO_PROVIDER: dict[str, str] = {
    "github.com": "github",
    "gitlab.com": "gitlab",
    "bitbucket.org": "bitbucket",
}

# Token placeholders for authenticated clone URLs
CLONE_TOKEN_FORMATS: dict[str, str] = {
    "github": "x-access-token",
    "gitlab": "oauth2",
    "bitbucket": "x-token-auth",
    "custom": "x-access-token",  # most self-hosted use GitHub-compatible format
}


def detect_provider_type(repo_url: str) -> str | None:
    """Auto-detect git provider type from a repository URL."""
    parsed = urlparse(repo_url)
    hostname = (parsed.hostname or "").lower()
    for host_pattern, provider_type in HOST_TO_PROVIDER.items():
        if hostname == host_pattern or hostname.endswith(f".{host_pattern}"):
            return provider_type
    return None


def parse_repo_url(repo_url: str) -> tuple[str | None, str | None]:
    """Extract (owner, repo) from a git repository URL.

    Handles HTTPS and SSH formats. Owner may contain slashes for GitLab subgroups.
    """
    if not repo_url:
        return None, None

    # SSH: git@host:owner/repo.git
    ssh_match = re.match(r"git@[^:]+:(.+)/([^/]+?)(?:\.git)?$", repo_url)
    if ssh_match:
        return ssh_match.group(1), ssh_match.group(2)

    # HTTPS
    parsed = urlparse(repo_url)
    path = parsed.path.strip("/")
    path = re.sub(r"\.git$", "", path)
    parts = path.split("/")

    if len(parts) < 2:
        return None, None

    repo = parts[-1]
    owner = "/".join(parts[:-1])
    return owner, repo


def build_clone_url(
    repo_url: str,
    token: str | None = None,
    provider_type: str | None = None,
) -> str:
    """Build an authenticated HTTPS clone URL.

    If no token, returns the original URL as-is.
    """
    if not token:
        return repo_url

    if not provider_type:
        provider_type = detect_provider_type(repo_url) or "github"

    parsed = urlparse(repo_url)

    # SSH URL - convert to HTTPS first
    ssh_match = re.match(r"git@([^:]+):(.+?)(?:\.git)?$", repo_url)
    if ssh_match:
        host = ssh_match.group(1)
        path = ssh_match.group(2)
        user_prefix = CLONE_TOKEN_FORMATS.get(provider_type, "x-access-token")
        return f"https://{user_prefix}:{token}@{host}/{path}.git"

    # HTTPS URL - inject token
    if parsed.scheme in ("https", "http"):
        user_prefix = CLONE_TOKEN_FORMATS.get(provider_type, "x-access-token")
        host = parsed.hostname or ""
        port_str = f":{parsed.port}" if parsed.port and parsed.port != 443 else ""
        path = parsed.path
        if not path.endswith(".git"):
            path = path.rstrip("/") + ".git"
        return f"https://{user_prefix}:{token}@{host}{port_str}{path}"

    return repo_url


def create_git_provider(
    provider_type: str | None = None,
    api_base_url: str | None = None,
    token: str | None = None,
    repo_url: str | None = None,
) -> GitProvider:
    """Create a GitProvider instance.

    Auto-detects provider type from repo_url if not specified.
    Defaults to GitHub for backward compatibility.
    """
    if not provider_type and repo_url:
        provider_type = detect_provider_type(repo_url)

    if not provider_type:
        provider_type = "github"

    base_url = api_base_url or DEFAULT_API_URLS.get(provider_type)

    match provider_type:
        case "github":
            return GitHubProvider(
                api_base_url=base_url or DEFAULT_API_URLS["github"],
                token=token,
            )
        case "gitlab":
            return GitLabProvider(
                api_base_url=base_url or DEFAULT_API_URLS["gitlab"],
                token=token,
            )
        case "bitbucket":
            return BitbucketProvider(
                api_base_url=base_url or DEFAULT_API_URLS["bitbucket"],
                token=token,
            )
        case "custom":
            if not base_url:
                raise ValueError("Custom git provider requires an API base URL")
            # Default to GitHub-compatible API (covers Gitea, Forgejo, etc.)
            return GitHubProvider(api_base_url=base_url, token=token)
        case _:
            raise ValueError(f"Unknown git provider type: {provider_type}")
