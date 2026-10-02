"""scheduler 周期同步循环的韧性测试：单次异常不能终止循环。"""

import asyncio
import os

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from app.core.config import settings
from app.services import scheduler


@pytest.mark.asyncio
async def test_periodic_sync_loop_survives_db_failure(monkeypatch):
    calls = {"failures": 0, "iterations": 0}

    def broken_session_local():
        calls["failures"] += 1
        if calls["failures"] <= 1:
            raise RuntimeError("connection refused")
        # 第二轮循环触发退出条件
        monkeypatch.setattr(settings, "auto_sync_enabled", False)
        raise RuntimeError("still down")

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        # 不真正等待退避时间
        await real_sleep(0)

    monkeypatch.setattr(scheduler, "SessionLocal", broken_session_local)
    monkeypatch.setattr(scheduler.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(settings, "auto_sync_enabled", True)

    # 循环必须正常返回而不是抛异常退出
    await scheduler.periodic_repo_sync_loop()
    assert calls["failures"] >= 2, "循环应在首次异常后退避重试而不是终止"
