import asyncio
import logging

from app.core.config import settings
from app.db.models import Repository
from app.db.session import SessionLocal
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository_by_id

logger = logging.getLogger(__name__)


async def periodic_repo_sync_loop() -> None:
    await asyncio.sleep(5)
    failures = 0
    while settings.auto_sync_enabled:
        try:
            db = SessionLocal()
            try:
                repos = db.query(Repository).filter(Repository.provider.in_(["github", "github_compatible"])).all()
                repo_ids = [repo.id for repo in repos]
            finally:
                db.close()

            for repo_id in repo_ids:
                try:
                    await sync_repository_by_id(repo_id, RepoSyncRequest(limit=settings.auto_sync_limit))
                except Exception as exc:
                    logger.warning("Periodic repo sync failed for %s: %s", repo_id, exc)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # DB 闪断等异常不能终止循环：指数退避后继续。
            failures += 1
            delay = min(60 * failures, 600)
            logger.exception("周期同步循环异常（第 %s 次），%s 秒后重试：%s", failures, delay, exc)
            await asyncio.sleep(delay)
            continue

        await asyncio.sleep(max(settings.auto_sync_interval_seconds, 30))
