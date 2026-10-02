# DevFlow AI 后端：uv 管理 Python 依赖
FROM python:3.12-slim

# git 供仓库克隆/worktree，ripgrep 供代码搜索，curl 供健康检查
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /usr/local/bin/

WORKDIR /app

# 先装依赖（利用镜像层缓存），再拷贝源码
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    REPO_CHECKOUT_DIR=/data/repos \
    UPLOAD_DIR=/data/uploads

RUN mkdir -p /data/repos /data/uploads

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
