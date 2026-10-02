"""轻量 API Key 鉴权中间件（纯 ASGI，避免影响 SSE 流式响应）。

规则：
- 未配置 DEVFLOW_API_KEY 时放行（本地裸开发模式），启动时打印警告。
- 只保护 /api/* 路径；/api/webhooks/* 豁免（走 GitHub HMAC 签名校验）。
- 非交互端点（/health、/docs、/openapi.json、/redoc）豁免。
- OPTIONS（CORS 预检）放行，预检不带凭证，实际请求仍会被校验。
- 接受 X-API-Key 头或 Authorization: Bearer <key>。
"""

import hmac
import logging

from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

_EXEMPT_PREFIXES = ("/api/webhooks",)
_EXEMPT_PATHS = {"/health", "/docs", "/redoc", "/openapi.json"}


class ApiKeyMiddleware:
    def __init__(self, app, *, api_key: str | None) -> None:
        self.app = app
        self.api_key = api_key

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not self.api_key:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        method = scope.get("method", "")
        if method == "OPTIONS" or path in _EXEMPT_PATHS or not path.startswith("/api"):
            await self.app(scope, receive, send)
            return
        if any(path.startswith(prefix) for prefix in _EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return
        provided = ""
        for key, value in scope.get("headers", []):
            name = key.decode("latin-1").lower()
            if name == "x-api-key":
                provided = value.decode("latin-1")
                break
            if name == "authorization":
                auth = value.decode("latin-1")
                if auth.lower().startswith("bearer "):
                    provided = auth[7:].strip()
                    break
        if provided and hmac.compare_digest(provided, self.api_key):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"detail": "缺少或无效的 API Key（X-API-Key）"}, status_code=401)
        await response(scope, receive, send)
