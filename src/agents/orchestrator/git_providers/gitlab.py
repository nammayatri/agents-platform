"""GitLab git provider implementation."""

from urllib.parse import quote_plus

import httpx

from .base import GitProvider

SKIP_DIRS = ("node_modules/", "vendor/", ".git/", "dist/", "__pycache__/", ".venv/")


class GitLabProvider(GitProvider):
    provider_type = "gitlab"

    def __init__(self, api_base_url: str = "https://gitlab.com", token: str | None = None):
        self.api_base_url = api_base_url.rstrip("/")
        headers: dict[str, str] = {}
        if token:
            headers["PRIVATE-TOKEN"] = token
        self.http = httpx.AsyncClient(timeout=30, headers=headers)

    def _project_path(self, owner: str, repo: str) -> str:
        return quote_plus(f"{owner}/{repo}")

    async def list_files(self, owner, repo, *, branch="HEAD", extensions=None):
        project_path = self._project_path(owner, repo)
        paths: list[str] = []
        page = 1
        per_page = 100
        while True:
            resp = await self.http.get(
                f"{self.api_base_url}/api/v4/projects/{project_path}/repository/tree",
                params={
                    "recursive": "true",
                    "per_page": per_page,
                    "page": page,
                    "ref": branch,
                },
            )
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break
            for item in items:
                if item["type"] != "blob":
                    continue
                if any(skip in item["path"].lower() for skip in SKIP_DIRS):
                    continue
                if extensions:
                    if not any(item["path"].lower().endswith(ext) for ext in extensions):
                        continue
                paths.append(item["path"])
            if len(items) < per_page:
                break
            page += 1
        return paths

    async def get_file_content(self, owner, repo, path, *, branch="HEAD"):
        project_path = self._project_path(owner, repo)
        encoded_path = quote_plus(path)
        resp = await self.http.get(
            f"{self.api_base_url}/api/v4/projects/{project_path}/repository/files/{encoded_path}/raw",
            params={"ref": branch},
        )
        resp.raise_for_status()
        return resp.text

    async def create_pull_request(self, owner, repo, *, head, base, title, body):
        project_path = self._project_path(owner, repo)
        resp = await self.http.post(
            f"{self.api_base_url}/api/v4/projects/{project_path}/merge_requests",
            json={
                "source_branch": head,
                "target_branch": base,
                "title": title,
                "description": body,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return {"url": data["web_url"], "number": data["iid"]}

    async def get_pull_request(self, owner, repo, number):
        project_path = self._project_path(owner, repo)
        resp = await self.http.get(
            f"{self.api_base_url}/api/v4/projects/{project_path}/merge_requests/{number}"
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "state": data["state"],
            "mergeable": data.get("merge_status") == "can_be_merged",
            "head_sha": data["sha"],
            "title": data["title"],
        }

    async def list_pr_reviews(self, owner, repo, number):
        project_path = self._project_path(owner, repo)
        resp = await self.http.get(
            f"{self.api_base_url}/api/v4/projects/{project_path}/merge_requests/{number}/approval_state"
        )
        resp.raise_for_status()
        data = resp.json()
        reviews = []
        for rule in data.get("rules", []):
            for user in rule.get("approved_by", []):
                reviews.append({
                    "state": "APPROVED",
                    "user": user.get("username", ""),
                    "body": "",
                    "submitted_at": "",
                })
        # Also fetch notes for review comments
        notes_resp = await self.http.get(
            f"{self.api_base_url}/api/v4/projects/{project_path}/merge_requests/{number}/notes",
            params={"sort": "desc", "per_page": 50},
        )
        if notes_resp.status_code == 200:
            for note in notes_resp.json():
                if note.get("system"):
                    continue
                reviews.append({
                    "state": "COMMENTED",
                    "user": note["author"]["username"],
                    "body": note.get("body", ""),
                    "submitted_at": note.get("created_at", ""),
                })
        return reviews

    async def merge_pull_request(self, owner, repo, number, *, method="squash"):
        project_path = self._project_path(owner, repo)
        json_body: dict = {}
        if method == "squash":
            json_body["squash"] = True
        resp = await self.http.put(
            f"{self.api_base_url}/api/v4/projects/{project_path}/merge_requests/{number}/merge",
            json=json_body,
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "merged": data.get("state") == "merged",
            "sha": data.get("merge_commit_sha", ""),
            "message": f"Merge request !{number} merged",
        }

    async def get_check_runs(self, owner, repo, ref):
        project_path = self._project_path(owner, repo)
        resp = await self.http.get(
            f"{self.api_base_url}/api/v4/projects/{project_path}/pipelines",
            params={"sha": ref, "per_page": 5},
        )
        resp.raise_for_status()
        pipelines = resp.json()
        if not pipelines:
            return {"state": "success", "checks": []}
        latest = pipelines[0]
        status = latest.get("status", "pending")
        state_map = {
            "success": "success",
            "failed": "failure",
            "canceled": "failure",
            "running": "pending",
            "pending": "pending",
            "created": "pending",
        }
        return {
            "state": state_map.get(status, "pending"),
            "checks": [{"name": f"pipeline-{latest['id']}", "status": status, "conclusion": status}],
        }

    async def post_pr_comment(self, owner, repo, number, body):
        project_path = self._project_path(owner, repo)
        resp = await self.http.post(
            f"{self.api_base_url}/api/v4/projects/{project_path}/merge_requests/{number}/notes",
            json={"body": body},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "id": data["id"],
            "url": f"{self.api_base_url}/{owner}/{repo}/-/merge_requests/{number}#note_{data['id']}",
        }

    async def health_check(self, owner, repo):
        try:
            project_path = self._project_path(owner, repo)
            resp = await self.http.get(
                f"{self.api_base_url}/api/v4/projects/{project_path}"
            )
            return resp.status_code == 200
        except Exception:
            return False
