import asyncio
import threading
import weakref
from collections.abc import Callable, Generator
from typing import Any

from sqlalchemy import StaticPool, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


class Base(DeclarativeBase):
    pass


def _build_engine(url: str):
    # SQLite（测试/本地演示）默认禁止跨线程使用连接；async 路由会把 Session
    # 带进线程池（to_thread），因此对 SQLite 显式放宽。内存库必须用 StaticPool
    # 共享同一个连接，否则不同线程会各自看到空库。
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if ":memory:" in url or "mode=memory" in url:
            return create_engine(url, poolclass=StaticPool, connect_args=connect_args)
        return create_engine(url, pool_pre_ping=True, connect_args=connect_args)
    # PostgreSQL：行锁等待 10 秒超时。并发同步撞锁时抛错返回，而不是无限等待——
    # 同步驱动在事件循环线程上等锁会卡死整个服务。
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"options": "-c lock_timeout=10000"},
    )


engine = _build_engine(settings.database_url)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# SQLAlchemy Session 不允许并发跨线程使用：工作流并行任务可能把同一个请求级
# Session 同时带进多个工作线程。这里按 Session 维护 asyncio 锁，保证同一
# Session 任一时刻只在一个线程里；不同请求的 Session 互不影响。
_session_locks: "weakref.WeakKeyDictionary[Session, asyncio.Lock]" = weakref.WeakKeyDictionary()
_session_locks_guard = threading.Lock()


async def to_thread_serialized(db: Session, fn: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """asyncio.to_thread 的 Session 安全版：同一 Session 的线程借用串行执行。"""
    lock = _session_locks.get(db)
    if lock is None:
        with _session_locks_guard:
            lock = _session_locks.get(db)
            if lock is None:
                lock = asyncio.Lock()
                _session_locks[db] = lock
    async with lock:
        return await asyncio.to_thread(fn, *args, **kwargs)
