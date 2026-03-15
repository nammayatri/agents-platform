"""GitHub git provider implementation."""

import httpx

from .base import GitProvider

SKIP_DIRS = ("node_modules/", "vendor/", ".git/", "dist/", "__pycache__/", ".venv/")


class GitHubProvider(GitProvider):
    provider_type = "github"

    def __init__(self, api_base_url: str = "https://api.github.com", token: str | None = None):
        self.api_base_url = api_base_url.rstrip("/")
        headers: dict[str, str] = {"Accept": "application/vnd.github.v3+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(timeout=30, headers=headers)

    async def list_files(self, owner, repo, *, branch="HEAD", extensions=None):
        resp = await self.http.get(
            f"{self.api_base_url}/repos/{owner}/{repo}/git/trees/{branch}",
            params={"recursive": "1"},
        )
        resp.raise_for_status()
        tree = resp.json()
        paths = []
        for item in tree.get("tree", []):
            if item["type"] != "blob":
                continue
            if any(skip in item["path"].lower() for skip in SKIP_DIRS):
                continue
            if extensions:
                if not any(item["path"].lower().endswith(ext) for ext in extensions):
                    continue
            paths.append(item["path"])
        return paths

    async def get_file_content(self, owner, repo, path, *, branch="HEAD"):
        resp = await self.http.get(
            f"{self.api_base_url}/repos/{owner}/{repo}/contents/{path}",
            params={"ref": branch},
            headers={"Accept": "application/vnd.github.v3.raw"},
        )
        resp.raise_for_status()
        return resp.text

    async def create_pull_request(self, owner, repo, *, head, base, title, body):
        resp = await self.http.post(
            f"{self.api_base_url}/repos/{owner}/{repo}/pulls",
            json={"head": head, "base": base, "title": title, "body": body},
        )
        resp.raise_for_status()
        data = resp.json()
        return {"url": data["html_url"], "number": data["number"]}

    async def get_pull_request(self, owner, repo, number):
        resp = await self.http.get(
            f"{self.api_base_url}/repos/{owner}/{repo}/pulls/{number}"
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "state": data["state"],
            "mergeable": data.get("mergeable"),
            "head_sha": data["head"]["sha"],
            "title": data["title"],
        }

    async def list_pr_reviews(self, owner, repo, number):
        resp = await self.http.get(
            f"{self.api_base_url}/repos/{owner}/{repo}/pulls/{number}/reviews"
        )
        resp.raise_for_status()
        return [
            {
                "state": r["state"],
                "user": r["user"]["login"],
                "body": r.get("body", ""),
                "submitted_at": r.get("submitted_at", ""),
            }
            for r in resp.json()
        ]

    async def merge_pull_request(self, owner, repo, number, *, method="squash"):
        resp = await self.http.put(
            f"{self.api_base_url}/repos/{owner}/{repo}/pulls/{number}/merge",
            json={"merge_method": method},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "merged": data.get("merged", True),
            "sha": data.get("sha", ""),
            "message": data.get("message", "Pull request merged"),
        }

    async def get_check_runs(self, owner, repo, ref):
        resp = await self.http.get(
            f"{self.api_base_url}/repos/{owner}/{repo}/commits/{ref}/check-runs"
        )
        resp.raise_for_status()
        data = resp.json()
        checks = [
            {
                "name": c["name"],
                "status": c["status"],
                "conclusion": c.get("conclusion"),
            }
            for c in data.get("check_runs", [])
        ]
        # Derive overall state
        if any(c["status"] != "completed" for c in checks):
            state = "pending"
        elif any(c["conclusion"] not in ("success", "skipped", "neutral") for c in checks):
            state = "failure"
        else:
            state = "success"
        return {"state": state, "checks": checks}

    async def post_pr_comment(self, owner, repo, number, body):
        resp = await self.http.post(
            f"{self.api_base_url}/repos/{owner}/{repo}/issues/{number}/comments",
            json={"body": body},
        )
        resp.raise_for_status()
        data = resp.json()
        return {"id": data["id"], "url": data["html_url"]}

    async def health_check(self, owner, repo):
        try:
            resp = await self.http.get(f"{self.api_base_url}/repos/{owner}/{repo}")
            return resp.status_code == 200
        except Exception:
            return False
