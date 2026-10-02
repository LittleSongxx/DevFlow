import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import Base, Document, Issue, Repository, WorkflowRun
from app.schemas.repos import RepoSyncRequest
from app.services.repos_sync import sync_repository


def _db_session():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


@pytest.mark.asyncio
async def test_sync_repository_persists_facts_without_triggering_rag(monkeypatch) -> None:
    async def fake_list_issues(*_args, **_kwargs):
        return [
            {
                "id": 7800,
                "number": 78,
                "title": "Error: Could not import 'app'.",
                "body": "Realistic issue body",
                "state": "open",
                "labels": [{"name": "bug"}],
                "user": {"login": "contributor"},
                "assignees": [],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
                "closed_at": None,
            }
        ]

    def fail_if_vector_runtime_is_used(*_args, **_kwargs):
        raise AssertionError("GitHub fact sync must not depend on embeddings")

    monkeypatch.setattr("app.services.repos_sync.list_issues", fake_list_issues)
    monkeypatch.setattr("app.services.rag.vector_store.embed_texts", fail_if_vector_runtime_is_used)
    monkeypatch.setattr("app.services.rag.vector_store.embed_texts", fail_if_vector_runtime_is_used)
    monkeypatch.setattr("app.services.rag.vector_store.embed_text", fail_if_vector_runtime_is_used)

    db = _db_session()
    repo = Repository(
        owner="openai",
        name="openai-quickstart-python",
        full_name="openai/openai-quickstart-python",
        provider="github",
        api_base_url="https://api.github.com",
    )
    db.add(repo)
    db.commit()

    result = await sync_repository(
        db,
        repo,
        RepoSyncRequest(sync_pull_requests=False, sync_workflow_runs=False, limit=1),
    )

    issue = db.query(Issue).filter(Issue.repo_id == repo.id).one()
    assert result.status == "completed"
    assert result.synced == {"issues": 1, "pull_requests": 0, "workflow_runs": 0}
    assert issue.number == 78
    assert issue.title == "Error: Could not import 'app'."
    assert db.query(Document).filter(Document.repo_id == repo.id).count() == 0


@pytest.mark.asyncio
async def test_sync_persists_workflow_runs_and_batch_survives_bad_rows(monkeypatch) -> None:
    """回归：远端 run 缺 status 字段时必须回退 unknown；单条失败不得回滚整批同步数据。"""

    async def fake_list_issues(*_args, **_kwargs):
        return [
            {
                "id": 1001,
                "number": 1,
                "title": "First issue",
                "state": "open",
                "labels": [],
                "user": {"login": "a"},
                "assignees": [],
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-02T00:00:00Z",
                "closed_at": None,
            }
        ]

    async def fake_list_pull_requests(*_args, **_kwargs):
        return []

    async def fake_list_workflow_runs(*_args, **_kwargs):
        return [
            # 两条 run 都缺 status 字段：修复前会触发 NOT NULL 违反并丢弃整批数据
            {"id": 555001, "name": "CI", "conclusion": "failure", "created_at": "2024-02-01T00:00:00Z"},
            {"id": 555002, "name": "CI", "conclusion": "success", "created_at": "2024-02-02T00:00:00Z"},
        ]

    async def fake_list_workflow_run_jobs(*_args, **_kwargs):
        return []

    def _no_vector(*_args, **_kwargs):
        raise AssertionError("fact sync must not touch vector runtime")

    monkeypatch.setattr("app.services.repos_sync.list_issues", fake_list_issues)
    monkeypatch.setattr("app.services.repos_sync.list_pull_requests", fake_list_pull_requests)
    monkeypatch.setattr("app.services.repos_sync.list_workflow_runs", fake_list_workflow_runs)
    monkeypatch.setattr("app.services.repos_sync.list_workflow_run_jobs", fake_list_workflow_run_jobs)
    monkeypatch.setattr("app.services.rag.vector_store.embed_texts", _no_vector)
    monkeypatch.setattr("app.services.rag.vector_store.embed_texts", _no_vector)
    monkeypatch.setattr("app.services.rag.vector_store.embed_text", _no_vector)

    db = _db_session()
    repo = Repository(owner="o", name="r", full_name="o/r", provider="github", api_base_url="https://api.github.com")
    db.add(repo)
    db.commit()

    result = await sync_repository(
        db,
        repo,
        RepoSyncRequest(sync_pull_requests=False, sync_workflow_runs=True, limit=5),
    )

    db2 = sessionmaker(bind=db.get_bind())()
    try:
        assert db2.query(Issue).filter(Issue.repo_id == repo.id).count() == 1, "Issue 数据不能被 run 同步失败连带回滚"
        runs = db2.query(WorkflowRun).filter(WorkflowRun.repo_id == repo.id).order_by(WorkflowRun.github_run_id).all()
        assert len(runs) == 2
        assert all(run.status == "unknown" for run in runs), "缺 status 时应回退 unknown"
        assert result.synced["workflow_runs"] == 2
    finally:
        db2.close()
