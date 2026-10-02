import hashlib
import hmac
import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository_by_id

logger = logging.getLogger(__name__)

router = APIRouter()


def verify_signature(body: bytes, signature: str | None) -> None:
    if not settings.github_webhook_secret:
        # 未配置 secret 时仅在开发模式放行；生产环境由启动校验直接拒绝。
        logger.warning("GITHUB_WEBHOOK_SECRET 未配置，webhook 未验签（仅限本地开发）。")
        return
    if not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=401, detail="缺少 GitHub webhook 签名")
    expected = hmac.new(settings.github_webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    actual = signature.removeprefix("sha256=")
    if not hmac.compare_digest(expected, actual):
        raise HTTPException(status_code=401, detail="GitHub webhook 签名无效")


def find_repo_by_full_name(full_name: str) -> Repository | None:
    db: Session = SessionLocal()
    try:
        return db.query(Repository).filter(Repository.full_name == full_name).one_or_none()
    finally:
        db.close()


@router.post("/github")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    body = await request.body()
    verify_signature(body, x_hub_signature_256)
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="webhook payload 不是合法的 JSON") from exc
    repository = payload.get("repository") or {}
    full_name = repository.get("full_name")
    if not full_name:
        return {"accepted": False, "reason": "payload.repository.full_name is missing"}

    repo = find_repo_by_full_name(full_name)
    if repo is None:
        # 统一返回 accepted=False，不区分“未连接”细节，避免被用来枚举已接入仓库。
        return {"accepted": False, "reason": "repository is not connected"}

    if x_github_event in {"issues", "pull_request", "workflow_run", "push", "check_suite", "check_run"}:
        background_tasks.add_task(sync_repository_by_id, repo.id, RepoSyncRequest(limit=settings.auto_sync_limit))
        return {"accepted": True, "event": x_github_event, "repo_id": str(repo.id), "action": "sync_scheduled"}

    return {"accepted": True, "event": x_github_event, "repo_id": str(repo.id), "action": "ignored"}
