"""安全修复的回归测试：API Key 鉴权、脱敏、git ref 校验、SSRF 防护、webhook 验签。"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.core.api_key import ApiKeyMiddleware
from app.core.config import settings
from app.core.redaction import sanitize_error, sanitize_text
from app.services.code_analysis import safe_git_ref
from app.services.github.client import GitHubClient, GitHubClientError, validate_api_base_url


@pytest.fixture()
def protected_app(monkeypatch):
    monkeypatch.setattr(settings, "devflow_api_key", "test-key-123")
    app = FastAPI()

    @app.get("/api/repos")
    async def repos():
        return {"ok": True}

    @app.post("/api/webhooks/github")
    async def webhook():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    app.add_middleware(ApiKeyMiddleware, api_key=settings.devflow_api_key)
    return TestClient(app)


def test_api_key_middleware_blocks_without_key(protected_app):
    response = protected_app.get("/api/repos")
    assert response.status_code == 401


def test_api_key_middleware_accepts_valid_key(protected_app):
    assert protected_app.get("/api/repos", headers={"X-API-Key": "test-key-123"}).status_code == 200
    assert protected_app.get("/api/repos", headers={"Authorization": "Bearer test-key-123"}).status_code == 200


def test_api_key_middleware_exempts_health_webhook_and_preflight(protected_app):
    assert protected_app.get("/health").status_code == 200
    assert protected_app.post("/api/webhooks/github").status_code == 200
    # CORS 预检不带凭证，必须放行（真实应用由 CORSMiddleware 应答；此处只验证不被 401 拦截）
    preflight = protected_app.options("/api/repos", headers={"Origin": "http://localhost:3001"})
    assert preflight.status_code != 401


def test_api_key_middleware_disabled_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "devflow_api_key", None)
    app = FastAPI()

    @app.get("/api/repos")
    async def repos():
        return {"ok": True}

    app.add_middleware(ApiKeyMiddleware, api_key=None)
    assert TestClient(app).get("/api/repos").status_code == 200


def test_sanitize_error_strips_tokens():
    message = sanitize_error(
        "Command '['git', 'clone', 'https://x-access-token:ghp_ABCDEFGH12345678@github.com/o/r.git']' "
        "returned non-zero exit status 128. sk-abcdef1234567890 Bearer sk-xyz987654321"
    )
    assert "ghp_ABCDEFGH12345678" not in message
    assert "sk-abcdef1234567890" not in message
    assert "x-access-token:" not in message
    assert "@github.com" in message  # 其余错误信息保留，便于排障


def test_sanitize_text_limits_length():
    assert len(sanitize_text("x" * 5000, limit=100)) == 100


def test_safe_git_ref_rejects_option_injection():
    with pytest.raises(RuntimeError):
        safe_git_ref("--upload-pack=touch /tmp/pwned")
    with pytest.raises(RuntimeError):
        safe_git_ref("a..b")
    with pytest.raises(RuntimeError):
        safe_git_ref("refs/@{evil}")
    assert safe_git_ref("feature/login-fix") == "feature/login-fix"
    assert safe_git_ref("main") == "main"


def test_github_client_rejects_unlisted_api_host(monkeypatch):
    monkeypatch.setattr(settings, "github_api_allowed_hosts", "api.github.com")
    with pytest.raises(GitHubClientError):
        GitHubClient(token=None, base_url="https://evil.example.com/api/v3")


def test_github_client_never_sends_global_token_to_custom_host(monkeypatch):
    monkeypatch.setattr(settings, "github_api_allowed_hosts", "ghe.corp.example.com")
    monkeypatch.setattr(settings, "github_token", "ghp_GLOBALSECRET_1234")
    client = GitHubClient(token=None, base_url="https://ghe.corp.example.com/api/v3")
    assert client.token is None, "全局 token 不应发给白名单外的自定义主机"


def test_github_client_allows_official_host_with_global_token(monkeypatch):
    monkeypatch.setattr(settings, "github_token", "ghp_GLOBALSECRET_1234")
    client = GitHubClient(token=None, base_url=None)
    assert client.token == "ghp_GLOBALSECRET_1234"


def test_webhook_signature_enforced(monkeypatch):
    import hashlib
    import hmac

    from app.api.routes.webhooks import verify_signature

    monkeypatch.setattr(settings, "github_webhook_secret", "whsec")
    body = b'{"repository": {"full_name": "a/b"}}'
    good = "sha256=" + hmac.new(b"whsec", body, hashlib.sha256).hexdigest()

    verify_signature(body, good)  # 合法签名放行

    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        verify_signature(body, "sha256=" + "0" * 64)
    with pytest.raises(HTTPException):
        verify_signature(body, None)
