"""把异常文本、日志与 HTTP 响应中的凭证模式统一抹除。

git 认证失败时 stderr 可能包含 x-access-token:...@ 形式的完整 clone URL，
CalledProcessError 的 str 会带完整命令行；这里提供出口收敛的脱敏工具。
"""

import re

_GITHUB_TOKEN_RE = re.compile(r"\bgh[pousr]_[A-Za-z0-9]{8,}")
_API_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9\-_]{8,}")
_TOKEN_IN_URL_RE = re.compile(r"(x-access-token|oauth2?|github-token):[^\s@/]+@", re.IGNORECASE)
_CREDENTIALS_IN_URL_RE = re.compile(r"(https?://)[^\s/@:]+:[^\s/@]+@", re.IGNORECASE)
_BEARER_RE = re.compile(r"(Bearer\s+)[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
_BASIC_AUTH_RE = re.compile(r"(Basic\s+)[A-Za-z0-9+/=]{8,}", re.IGNORECASE)


def sanitize_text(value: str | BaseException, *, limit: int = 600) -> str:
    text = str(value).strip()
    if not text:
        text = type(value).__name__ if isinstance(value, BaseException) else ""
    text = _TOKEN_IN_URL_RE.sub(r"\1:***@", text)
    text = _CREDENTIALS_IN_URL_RE.sub(r"\1***:***@", text)
    text = _GITHUB_TOKEN_RE.sub("ghp_***", text)
    text = _API_KEY_RE.sub("sk-***", text)
    text = _BEARER_RE.sub(r"\1***", text)
    text = _BASIC_AUTH_RE.sub(r"\1***", text)
    return text[:limit]


def sanitize_error(exc: BaseException | str, *, limit: int = 600) -> str:
    return sanitize_text(exc, limit=limit)
