import asyncio
import contextlib
import faulthandler
import logging
import signal
from collections.abc import AsyncIterator
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.routes import (
    action_drafts,
    chat,
    ci,
    code_graph,
    conversations,
    evaluations,
    feedback,
    issues,
    knowledge,
    project_index,
    pull_requests,
    rag,
    repos,
    reports,
    search,
    skills,
    webhooks,
    workspaces,
)
from app.core.api_key import ApiKeyMiddleware
from app.core.config import settings
from app.db.session import engine
from app.services.scheduler import periodic_repo_sync_loop
from app.services.rag.vector_store import VectorStoreUnavailableError

logger = logging.getLogger(__name__)

# kill -USR1 <pid> 可随时转储所有线程的 Python 栈，用于诊断事件循环/协程卡死。
_faulthandler_registered = False
try:
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    _faulthandler_registered = True
except (ValueError, OSError):  # pragma: no cover - 非主线程或平台不支持
    pass

# 启动迁移的 PostgreSQL advisory lock 常量，防止多 worker 同时 ALTER TABLE。
_MIGRATION_LOCK_KEY = 723751


def _cors_origins() -> list[str]:
    return [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]


def _cors_origin_regex() -> str | None:
    if settings.app_env.lower() == "production":
        return None
    return r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


def _validate_runtime_security() -> None:
    """生产环境的安全底线：弱默认值直接拒绝启动。"""
    if settings.app_env.lower() != "production":
        if not settings.devflow_api_key:
            logger.warning("DEVFLOW_API_KEY 未配置，/api/* 处于无鉴权模式（仅限本地开发）。")
        return
    problems: list[str] = []
    if not settings.devflow_api_key:
        problems.append("DEVFLOW_API_KEY is required in production")
    if settings.token_encryption_key == "change-me-in-local-dev":
        problems.append("TOKEN_ENCRYPTION_KEY must be changed from its default value")
    if not settings.github_webhook_secret:
        problems.append("GITHUB_WEBHOOK_SECRET is required in production")
    if problems:
        raise RuntimeError("Refusing to start in production: " + "; ".join(problems))


def _run_startup_migrations() -> None:
    # Alembic 迁移取代历史的启动期手工 DDL；advisory lock 防多 worker 并发迁移。
    backend_root = Path(__file__).resolve().parents[1]
    alembic_cfg = AlembicConfig(str(backend_root / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(backend_root / "alembic"))
    with contextlib.ExitStack() as stack:
        if engine.dialect.name == "postgresql":
            connection = stack.enter_context(engine.connect())
            connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": _MIGRATION_LOCK_KEY})
            stack.callback(lambda: connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _MIGRATION_LOCK_KEY}))
            connection.commit()
        alembic_command.upgrade(alembic_cfg, "head")


def _sweep_stale_background_jobs() -> None:
    """进程崩溃遗留的中间态任务标记为可重试的失败状态。"""
    from app.db.models import EvalRun, KnowledgeSourceDocument
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        for run in db.query(EvalRun).all():
            status = (run.result_json or {}).get("status")
            if status in {"queued", "running"}:
                result = dict(run.result_json or {})
                result["status"] = "error"
                result["error"] = "服务重启导致评测中断，请重新运行。"
                run.result_json = result
        for doc in db.query(KnowledgeSourceDocument).filter(
            KnowledgeSourceDocument.status.in_(["parsing", "splitting", "embedding"])
        ).all():
            doc.status = "failed"
            doc.error_message = "服务重启导致处理中断，可重试上传。"
        db.commit()
    except Exception:
        logger.exception("清扫遗留后台任务状态失败（不影响启动）")
        db.rollback()
    finally:
        db.close()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _validate_runtime_security()
    await asyncio.to_thread(_run_startup_migrations)
    await asyncio.to_thread(_sweep_stale_background_jobs)

    from app.db.session import SessionLocal
    from app.services.conversations import backfill_all_default_conversations
    from app.services.code_analysis import purge_legacy_code_documents

    def _post_migration_cleanup() -> None:
        db = SessionLocal()
        try:
            backfill_all_default_conversations(db)
            if purge_legacy_code_documents(db):
                db.commit()
        finally:
            db.close()

    await asyncio.to_thread(_post_migration_cleanup)
    if settings.auto_sync_enabled:
        app.state.repo_sync_task = asyncio.create_task(periodic_repo_sync_loop())
    try:
        yield
    finally:
        task = getattr(app.state, "repo_sync_task", None)
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)

    @app.exception_handler(VectorStoreUnavailableError)
    async def vector_store_unavailable_handler(_request, exc: VectorStoreUnavailableError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc), "service": "vector_store", "status": "degraded"},
        )
    app.add_middleware(ApiKeyMiddleware, api_key=settings.devflow_api_key)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_origin_regex=_cors_origin_regex(),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(repos.router, prefix="/api/repos", tags=["repos"])
    app.include_router(conversations.router, prefix="/api/repos", tags=["conversations"])
    app.include_router(workspaces.router, prefix="/api/workspaces", tags=["workspaces"])
    app.include_router(issues.router, prefix="/api", tags=["issues"])
    app.include_router(pull_requests.router, prefix="/api", tags=["pull_requests"])
    app.include_router(ci.router, prefix="/api", tags=["ci"])
    app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
    app.include_router(feedback.router, prefix="/api/chat/feedback", tags=["chat_feedback"])
    app.include_router(action_drafts.router, prefix="/api/action-drafts", tags=["action_drafts"])
    app.include_router(reports.router, prefix="/api/reports", tags=["reports"])
    app.include_router(search.router, prefix="/api/search", tags=["search"])
    app.include_router(skills.router, prefix="/api/skills", tags=["skills"])
    app.include_router(knowledge.router, prefix="/api/repos", tags=["knowledge"])
    app.include_router(project_index.router, prefix="/api/repos", tags=["project_index"])
    app.include_router(rag.router, prefix="/api/rag", tags=["rag"])
    app.include_router(code_graph.router, prefix="/api", tags=["code_graph"])
    app.include_router(evaluations.router, prefix="/api/evals", tags=["evaluations"])
    app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name}

    @app.get("/api/_debug/tasks")
    async def debug_tasks() -> dict:
        """转储所有 asyncio 任务的挂起栈（受 ApiKeyMiddleware 保护），用于诊断协程卡死。"""
        tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        dumped: dict[str, list[str]] = {}
        for task in tasks:
            stack = task.get_stack(limit=12)
            frames = [
                f"{frame.f_code.co_filename}:{frame.f_lineno} in {frame.f_code.co_name}"
                for frame in reversed(stack)
            ]
            dumped[f"{task.get_name()} :: {str(task.get_coro())[:100]}"] = frames
        return {"count": len(tasks), "tasks": dumped}

    return app


app = create_app()
