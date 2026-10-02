from typing import Any
from urllib.parse import urlparse

import httpx

from app.core.config import settings


class GitHubClientError(ValueError):
    """api_base_url 不在允许的主机白名单内。"""


def _allowed_api_hosts() -> set[str]:
    hosts = {host.strip().lower() for host in settings.github_api_allowed_hosts.split(",") if host.strip()}
    hosts.add("api.github.com")
    return hosts


def validate_api_base_url(base_url: str) -> str:
    normalized = (base_url or "https://api.github.com").rstrip("/")
    parsed = urlparse(normalized)
    host = (parsed.hostname or "").lower()
    local_http = (
        parsed.scheme == "http"
        and host in {"localhost", "127.0.0.1", "::1"}
        and settings.app_env.lower() != "production"
    )
    if (parsed.scheme != "https" and not local_http) or host not in _allowed_api_hosts():
        raise GitHubClientError(
            f"api_base_url 必须是 https 且主机在 GITHUB_API_ALLOWED_HOSTS 白名单内：{host or normalized}"
        )
    return normalized


class GitHubClient:
    def __init__(self, token: str | None = None, base_url: str | None = None) -> None:
        self.base_url = validate_api_base_url(base_url or settings.github_api_base_url)
        host = urlparse(self.base_url).hostname or ""
        # 全局 token 只允许发给官方 GitHub API，防止把服务端凭证发往任意主机。
        if token:
            self.token = token
        elif host == "api.github.com":
            self.token = settings.github_token
        else:
            self.token = None

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        # follow_redirects=False：跨域重定向不会携带 Authorization 头。
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.get(f"{self.base_url}{path}", headers=self._headers(), params=params)
            response.raise_for_status()
            return response.json()

    async def get_bytes(self, path: str, params: dict[str, Any] | None = None) -> bytes:
        """GET 字节流；手动跟随 3xx，且重定向到其他主机时剥离 Authorization。

        GitHub Actions 日志端点会 302 到带签名的对象存储 URL；手动跟随可以保证
        凭证只在白名单主机之间传递，不会泄露给重定向目标。
        """
        url = f"{self.base_url}{path}"
        headers = self._headers()
        async with httpx.AsyncClient(timeout=60, follow_redirects=False) as client:
            for _ in range(3):
                response = await client.get(url, headers=headers, params=params if _ == 0 else None)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    response.raise_for_status()
                    return response.content
                location = response.headers.get("location")
                if not location:
                    response.raise_for_status()
                    return response.content
                parsed_next = urlparse(location)
                if parsed_next.scheme not in {"http", "https"}:
                    raise GitHubClientError(f"非法的重定向目标：{location[:80]}")
                if (parsed_next.hostname or "").lower() != (urlparse(url).hostname or "").lower():
                    headers = {key: value for key, value in headers.items() if key != "Authorization"}
                url = location
            raise GitHubClientError("重定向次数超过上限")

    async def post(self, path: str, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.post(f"{self.base_url}{path}", headers=self._headers(), json=payload)
            response.raise_for_status()
            return response.json()

    async def patch(self, path: str, payload: dict[str, Any]) -> Any:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            response = await client.patch(f"{self.base_url}{path}", headers=self._headers(), json=payload)
            response.raise_for_status()
            return response.json()

    async def get_repo(self, owner: str, repo: str) -> dict[str, Any]:
        return await self.get(f"/repos/{owner}/{repo}")

    async def create_issue_comment(self, owner: str, repo: str, issue_number: int, body: str) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/comments", {"body": body})

    async def create_issue(self, owner: str, repo: str, title: str, body: str) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues", {"title": title, "body": body})

    async def close_issue(self, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        return await self.patch(f"/repos/{owner}/{repo}/issues/{issue_number}", {"state": "closed"})

    async def add_issue_labels(self, owner: str, repo: str, issue_number: int, labels: list[str]) -> dict[str, Any]:
        return await self.post(f"/repos/{owner}/{repo}/issues/{issue_number}/labels", {"labels": labels})
