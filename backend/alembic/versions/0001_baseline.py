"""幂等基线：等价于历史的启动期建表/补列/唯一约束逻辑。

- 全新数据库：create_all 建出全部表；
- 历史数据库（由旧版启动逻辑维护）：仅补齐缺失的列与唯一约束，已存在的对象不动。

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.db.session import Base  # noqa: F401  （触发全部模型注册）
import app.db.models  # noqa: F401  （确保 create_all 覆盖全部表）

revision: str = "0001_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMN_ADDITIONS: list[tuple[str, str, str]] = [
    ("users", "role", "VARCHAR(80) DEFAULT 'owner'"),
    ("repositories", "provider", "VARCHAR(80) DEFAULT 'github'"),
    ("repositories", "api_base_url", "TEXT"),
    ("repositories", "clone_url", "TEXT"),
    ("repositories", "local_path", "TEXT"),
    ("repositories", "checkout_mode", "VARCHAR(80) DEFAULT 'managed'"),
    ("repositories", "github_token_encrypted", "TEXT"),
    ("repositories", "last_selected_at", "TIMESTAMP WITH TIME ZONE"),
    ("workflow_runs", "jobs", "JSON"),
    ("code_symbols", "pr_id", "UUID"),
    ("code_symbols", "pr_number", "INTEGER"),
    ("code_relations", "pr_id", "UUID"),
    ("code_relations", "pr_number", "INTEGER"),
    ("chat_messages", "conversation_id", "UUID"),
    ("chat_sessions", "conversation_id", "UUID"),
    ("agent_runs", "conversation_id", "UUID"),
    ("evidence_items", "conversation_id", "UUID"),
    ("thread_memories", "facts", "JSON"),
    ("thread_memories", "tasks", "JSON"),
    ("thread_memories", "user_preferences", "JSON"),
    ("thread_memories", "repo_context", "JSON"),
    ("thread_memories", "citations", "JSON"),
    ("conversation_memories", "facts", "JSON"),
    ("conversation_memories", "tasks", "JSON"),
    ("conversation_memories", "user_preferences", "JSON"),
    ("conversation_memories", "repo_context", "JSON"),
    ("conversation_memories", "citations", "JSON"),
]

_UNIQUE_CONSTRAINTS: list[tuple[str, str, str]] = [
    ("issues", "repo_id,github_issue_id", "uq_issues_repo_github"),
    ("pull_requests", "repo_id,github_pr_id", "uq_pull_requests_repo_github"),
    ("workflow_runs", "repo_id,github_run_id", "uq_workflow_runs_repo_github"),
]


def _add_column_if_missing(bind, inspector: sa.Inspector, table: str, column: str, ddl_type: str) -> None:
    if table not in inspector.get_table_names():
        return
    if column in {item["name"] for item in inspector.get_columns(table)}:
        return
    bind.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def _add_unique_constraint(bind, table: str, columns: str, name: str) -> None:
    """去重后加唯一约束；已存在或非 PostgreSQL 时跳过（与历史启动逻辑一致）。"""
    if bind.dialect.name != "postgresql":
        return
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": name}
    ).first()
    if exists:
        return
    first, second = columns.split(",")
    bind.execute(
        sa.text(
            f"DELETE FROM {table} a USING {table} b "
            f"WHERE a.{first} = b.{first} AND a.{second} = b.{second} "
            f"AND a.{second} IS NOT NULL AND a.id > b.id"
        )
    )
    bind.execute(sa.text(f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE ({columns})"))


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    inspector = sa.inspect(bind)
    for table, column, ddl_type in _COLUMN_ADDITIONS:
        _add_column_if_missing(bind, inspector, table, column, ddl_type)
    for table, columns, name in _UNIQUE_CONSTRAINTS:
        _add_unique_constraint(bind, table, columns, name)


def downgrade() -> None:
    # 基线迁移不可回滚（会清空业务数据）。
    raise NotImplementedError("baseline migration cannot be reverted")
