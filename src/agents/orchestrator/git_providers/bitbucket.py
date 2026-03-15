"""Bitbucket git provider implementation."""

import httpx

from .base import GitProvider

SKIP_DIRS = ("node_modules/", "vendor/", ".git/", "dist/", "__pycache__/", ".venv/")


class BitbucketProvider(GitProvider):
    provider_type = "bitbucket"

    def __init__(self, api_base_url: str = "https://api.bitbucket.org", token: str | None = None):
        self.api_base_url = api_base_url.rstrip("/")
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.AsyncClient(timeout=30, headers=headers)

    async def list_files(self, owner, repo, *, branch="HEAD", extensions=None):
        paths: list[str] = []
        url: str | None = f"{self.api_base_url}/2.0/repositories/{owner}/{repo}/src/{branch}/"
        params: dict[str, str | int] = {"pagelen": 100}
        while url:
            resp = await self.http.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("values", []):
                if item.get("type") != "commit_file":
                    continue
                path = item["path"]
                if any(skip in path.lower() for skip in SKIP_DIRS):
                    continue
                if extensions:
                    if not any(path.lower().endswith(ext) for ext in extensions):
                        continue
                paths.append(path)
            url = data.get("next")
            params = {}  # next URL includes params
        return paths

    async def get_file_content(self, owner, repo, path, *, branch="HEAD"):
        resp = await self.http.get(
            f"{self.api_base_url}/2.0/repositories/{owner}/{repo}/src/{branch}/{path}"
        )
        resp.raise_for_status()
        return resp.text

    async def create_pull_request(self, owner, repo, *, head, base, title, body):
        resp = await self.http.post(
            f"{self.api_base_url}/2.0/repositories/{owner}/{repo}/pullrequests",
            json={
                "title": title,
                "description": body,
                "source": {"branch": {"name": head}},
                "destination": {"branch": {"name": base}},
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "url": data["links"]["html"]["href"],
            "number": data["id"],
        }

    async def get_pull_request(self, owner, repo, number):
        resp = await self.http.get(
            f"{self.api_base_url}/2.0/repositories/{owner}/{repo}/pullrequests/{number}"
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "state": data["state"],
            "mergeable": data["state"] == "OPEN",
            "head_sha": data["source"]["commit"]["hash"],
            "title": data["title"],
        }

    async def list_pr_reviews(self, owner, repo, number):
        resp = await self.http.get(
            f"{self.api_base_url}/2.0/repositories/{owner}/{repo}/pullrequests/{number}/activity"
        )
        resp.raise_for_status()
        reviews = []
        for item in resp.json().get("values", []):
            if "approval" in item:
                reviews.append({
                    "state": "APPROVED",
                    "user": item["approval"]["user"]["display_name"],
                    "body": "",
                    "submitted_at": item["approval"].get("date", ""),
                })
            elif "comment" in item:
                reviews.append({
                    "state": "COMMENTED",
                    "user": item["comment"]["user"]["display_name"],
                    "body": item["comment"].get("content", {}).get("raw", ""),
                    "submitted_at": item["comment"].get("created_on", ""),
                })
        return reviews

    async def merge_pull_request(self, owner, repo, number, *, method="squash"):
        json_body: dict = {}
        if method == "squash":
            json_body["merge_strategy"] = "squash"
        elif method == "merge":
            json_body["merge_strategy"] = "merge_commit"
        resp = await self.http.post(
            f"{self.api_base_url}/2.0/repositories/{owner}/{repo}/pullrequests/{number}/merge",
            json=json_body,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "merged": data.get("state") == "MERGED",
            "sha": data.get("merge_commit", {}).get("hash", ""),
            "message": f"Pull request #{number} merged",
        }

    async def get_check_runs(self, owner, repo, ref):
        resp = await self.http.get(
            f"{self.api_base_url}/2.0/repositories/{owner}/{repo}/commit/{ref}/statuses"
        )
        resp.raise_for_status()
        statuses = resp.json().get("values", [])
        checks = [
            {
                "name": s.get("name", s.get("key", "unknown")),
                "status": "completed" if s["state"] in ("SUCCESSFUL", "FAILED", "STOPPED") else "in_progress",
                "conclusion": "success" if s["state"] == "SUCCESSFUL" else "failure" if s["state"] in ("FAILED", "STOPPED") else None,
            }
            for s in statuses
        ]
        if not checks:
            state = "success"
        elif any(c["conclusion"] is None for c in checks):
            state = "pending"
        elif any(c["conclusion"] == "failure" for c in checks):
            state = "failure"
        else:
            state = "success"
        return {"state": state, "checks": checks}

    async def post_pr_comment(self, owner, repo, number, body):
        resp = await self.http.post(
            f"{self.api_base_url}/2.0/repositories/{owner}/{repo}/pullrequests/{number}/comments",
            json={"content": {"raw": body}},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "id": data["id"],
            "url": data.get("links", {}).get("html", {}).get("href", ""),
        }

    async def health_check(self, owner, repo):
        try:
            resp = await self.http.get(
                f"{self.api_base_url}/2.0/repositories/{owner}/{repo}"
            )
            return resp.status_code == 200
        except Exception:
            return False
