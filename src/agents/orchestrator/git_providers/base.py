"""Abstract git provider interface for fetching repository file trees, contents, and creating PRs."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class RepoFile:
    """A file fetched from a git repository."""
    path: str
    content: str


class GitProvider(ABC):
    """Abstract interface for git hosting providers."""

    provider_type: str

    @abstractmethod
    async def list_files(
        self,
        owner: str,
        repo: str,
        *,
        branch: str = "HEAD",
        extensions: list[str] | None = None,
    ) -> list[str]:
        """List file paths in the repository, optionally filtered by extension."""
        ...

    @abstractmethod
    async def get_file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        *,
        branch: str = "HEAD",
    ) -> str:
        """Fetch the raw content of a single file."""
        ...

    @abstractmethod
    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str,
    ) -> dict:
        """Create a pull request. Returns dict with 'url' and 'number' keys."""
        ...

    @abstractmethod
    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> dict:
        """Get PR details. Returns dict with 'state', 'mergeable', 'head_sha', 'title'."""
        ...

    @abstractmethod
    async def list_pr_reviews(
        self,
        owner: str,
        repo: str,
        number: int,
    ) -> list[dict]:
        """List reviews on a PR. Returns list of dicts with 'state', 'user', 'body', 'submitted_at'."""
        ...

    @abstractmethod
    async def merge_pull_request(
        self,
        owner: str,
        repo: str,
        number: int,
        *,
        method: str = "squash",
    ) -> dict:
        """Merge a PR. method is 'merge', 'squash', or 'rebase'. Returns dict with 'merged', 'sha', 'message'."""
        ...

    @abstractmethod
    async def get_check_runs(
        self,
        owner: str,
        repo: str,
        ref: str,
    ) -> dict:
        """Get CI check status for a ref. Returns dict with 'state' ('success'|'pending'|'failure') and 'checks' list."""
        ...

    @abstractmethod
    async def post_pr_comment(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
    ) -> dict:
        """Post a comment on a PR. Returns dict with 'id' and 'url'."""
        ...

    @abstractmethod
    async def health_check(self, owner: str, repo: str) -> bool:
        """Test connectivity to the repository."""
        ...
