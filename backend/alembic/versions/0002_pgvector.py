"""pgvector：启用扩展、documents.embedding 向量列与 HNSW 余弦索引。

SQLite（测试/本地演示）跳过——由 create_all 按 EmbeddingVector 类型建列
（JSON 文本退化），不参与向量检索。

Revision ID: 0002_pgvector
Revises: 0001_baseline
Create Date: 2026-10-02

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0002_pgvector"
down_revision: Union[str, None] = "0001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_pg_type(bind, table: str, column: str) -> str | None:
    row = bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod) AS coltype "
            "FROM pg_attribute a "
            "WHERE a.attrelid = CAST(:table AS regclass) AND a.attname = :column AND NOT a.attisdropped"
        ),
        {"table": table, "column": column},
    ).first()
    return row[0] if row else None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    dimensions = settings.embedding_dimensions
    existing_type = _column_pg_type(bind, "documents", "embedding")
    if existing_type is None:
        op.execute(f"ALTER TABLE documents ADD COLUMN embedding vector({dimensions})")
    elif "vector" not in existing_type.lower():
        # 历史遗留的非向量列（早期版本的 embedding 存储）：替换为 pgvector 列，
        # 丢失的向量可由 reindex 重建。
        op.execute("ALTER TABLE documents DROP COLUMN embedding")
        op.execute(f"ALTER TABLE documents ADD COLUMN embedding vector({dimensions})")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_documents_embedding "
        "ON documents USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_documents_embedding")
    op.execute("ALTER TABLE documents DROP COLUMN IF EXISTS embedding")
