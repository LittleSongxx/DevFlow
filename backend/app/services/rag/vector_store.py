import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Document
from app.db.session import SessionLocal, engine
from app.services.rag.embeddings import embed_text, embed_texts, embedding_contract, embedding_provider_name
from app.services.rag.source_policy import policy_metadata, should_vectorize

logger = logging.getLogger(__name__)


class VectorStoreUnavailableError(RuntimeError):
    """向量检索不可用：未启用、非 PostgreSQL 方言或 pgvector 未就绪。"""


@dataclass
class DocumentPayload:
    source_type: str
    title: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    source_id: uuid.UUID | str | None = None


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID:
    if value is None:
        return uuid.uuid4()
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def stable_source_id(repo_id: uuid.UUID | str, source_type: str, key: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, f"devflow:{repo_id}:{source_type}:{key}")


def _vector_literal(vector: list[float]) -> str:
    """把浮点列表编码成 pgvector 的文本字面量 '[1.0,2.0,...]'。"""
    return "[" + ",".join(repr(float(item)) for item in vector) + "]"


def _validate_embeddings(vectors: list[list[float]]) -> None:
    """写入前的维度契约校验：所有仓库共享同一向量空间（documents.embedding 列）。"""
    for vector in vectors:
        if len(vector) != settings.embedding_dimensions:
            raise ValueError(
                f"embedding has {len(vector)} dimensions; expected {settings.embedding_dimensions}. "
                "Refusing to truncate or pad vectors."
            )


def _require_vector_store() -> None:
    if not settings.vector_search_enabled:
        raise VectorStoreUnavailableError(
            "vector search is disabled; vector and hybrid RAG are unavailable"
        )
    if engine.dialect.name != "postgresql":
        raise VectorStoreUnavailableError(
            "pgvector retrieval requires PostgreSQL; current dialect is "
            f"{engine.dialect.name} (SQLite 测试/演示环境只保留关键词检索)"
        )


def _pgvector_ready() -> tuple[bool, str | None]:
    """检查 pgvector 扩展与 documents.embedding 列是否就绪，返回 (ready, error)。"""
    if not settings.vector_search_enabled:
        return False, "vector search is disabled"
    if engine.dialect.name != "postgresql":
        return False, f"pgvector requires PostgreSQL; current dialect is {engine.dialect.name}"
    try:
        with SessionLocal() as session:
            extension = session.execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first()
            if extension is None:
                return False, "pgvector extension is not installed; run database migrations"
            # 用 pg_attribute 查列存在性：SQLAlchemy 反射不识别 vector 类型会刷 SAWarning。
            column = session.execute(
                text(
                    "SELECT 1 FROM pg_attribute a "
                    "WHERE a.attrelid = 'documents'::regclass AND a.attname = 'embedding' "
                    "AND NOT a.attisdropped"
                )
            ).first()
            if column is None:
                return False, "documents.embedding column is missing; run database migrations"
    except Exception as exc:  # pragma: no cover - 依赖本地基础设施。
        return False, f"failed to inspect vector store: {exc}"
    return True, None


def vector_store_status() -> dict[str, Any]:
    ready, error = _pgvector_ready()
    if not ready:
        return {
            "backend": "pgvector",
            "status": "degraded",
            "available": False,
            "error": error,
            "embedding": embedding_contract(),
        }
    return {
        "backend": "pgvector",
        "status": "ready",
        "available": True,
        "error": None,
        "column": "documents.embedding",
        "index": "hnsw (vector_cosine_ops)",
        "embedding": embedding_contract(),
    }


def _vector_search_statement(
    repo_id: str | uuid.UUID,
    vector: list[float],
    source_type: str | None,
    source_types: list[str] | None,
    limit: int,
) -> tuple[text, dict[str, Any]]:
    """构造 pgvector 余弦检索语句与参数（独立出来便于对租户过滤做单元测试）。"""
    filters = ["repo_id = CAST(:repo_id AS uuid)", "embedding IS NOT NULL"]
    params: dict[str, Any] = {
        "repo_id": str(repo_id),
        "qv": _vector_literal(vector),
        "limit": max(1, int(limit)),
    }
    if source_type:
        filters.append("source_type = :source_type")
        params["source_type"] = source_type
    elif source_types:
        filters.append("source_type = ANY(:source_types)")
        params["source_types"] = list(source_types)
    statement = text(
        "SELECT id, title, source_type, source_id, metadata AS metadata_json, content, "
        "1 - (embedding <=> CAST(:qv AS vector)) AS similarity "
        f"FROM documents WHERE {' AND '.join(filters)} "
        "ORDER BY embedding <=> CAST(:qv AS vector) "
        "LIMIT :limit"
    )
    return statement, params


def search_documents_by_vector(
    repo_id: str | uuid.UUID,
    query: str,
    source_type: str | None = None,
    limit: int = 20,
    query_embedding: list[float] | None = None,
    source_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """在 documents.embedding（pgvector）上做余弦相似度检索。

    返回形状与历史向量检索保持一致：score 为余弦相似度（越大越好），
    消费方（retrieval 融合权重 0.62）的方向不变。
    """
    _require_vector_store()
    try:
        vector = query_embedding or embed_text(query, settings.embedding_dimensions)
        _validate_embeddings([vector])
        statement, params = _vector_search_statement(
            repo_id, vector, source_type, source_types, limit
        )
        with SessionLocal() as session:
            rows = session.execute(statement, params).mappings().all()
        output: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            content = str(row["content"] or "")
            output.append(
                {
                    "id": str(row["id"]),
                    "title": str(row["title"] or ""),
                    "source_type": str(row["source_type"] or ""),
                    "source_id": str(row["source_id"] or ""),
                    "score": round(float(row["similarity"]), 4),
                    "metadata": metadata,
                    "snippet": content[:260],
                    "_raw_content": content,
                }
            )
        return output
    except VectorStoreUnavailableError:
        raise
    except Exception as exc:  # pragma: no cover - 依赖本地数据库/维度契约。
        raise VectorStoreUnavailableError(f"failed to search documents by vector: {exc}") from exc


def add_document(
    db: Session,
    repo_id: str | uuid.UUID,
    source_type: str,
    source_id: str | uuid.UUID | None,
    title: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    *,
    commit: bool = True,
) -> Document:
    if source_type == "code_file":
        raise ValueError("源码必须通过工作区实时搜索，不能保存为 RAG Document")
    vectorized = should_vectorize(source_type)
    vectors = [embed_text(f"{title}\n{content}", settings.embedding_dimensions)] if vectorized else []
    if vectors:
        _validate_embeddings(vectors)
    embedding_metadata = (
        {
            "embedding_provider": embedding_provider_name(),
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
        }
        if vectorized
        else {}
    )
    doc = Document(
        repo_id=_as_uuid(repo_id),
        source_type=source_type,
        source_id=_as_uuid(source_id),
        title=title[:500],
        content=content,
        # 向量与文档同库同事务：embedding 计算失败会中止写入，不存在孤儿向量。
        embedding=vectors[0] if vectors else None,
        meta={
            **(metadata or {}),
            **policy_metadata(source_type),
            **embedding_metadata,
        },
    )
    db.add(doc)
    db.flush()
    if commit:
        db.commit()
        db.refresh(doc)
    return doc


def add_documents(
    db: Session,
    repo_id: str | uuid.UUID,
    payloads: list[DocumentPayload],
    *,
    commit: bool = True,
    vectors: list[list[float]] | None = None,
    embedding_info: dict[str, Any] | None = None,
    generate_embeddings: bool = True,
    sync_vector_store: bool = True,
) -> list[Document]:
    clean_payloads = [payload for payload in payloads if payload.content.strip()]
    if any(payload.source_type == "code_file" for payload in clean_payloads):
        raise ValueError("源码必须通过工作区实时搜索，不能保存为 RAG Document")
    if vectors is not None and len(vectors) != len(clean_payloads):
        raise ValueError("embedding count does not match document payload count")

    embeddings: list[list[float] | None] = [None] * len(clean_payloads)
    vector_indexes = [
        index for index, payload in enumerate(clean_payloads) if should_vectorize(payload.source_type)
    ]
    if vectors is not None:
        for index in vector_indexes:
            embeddings[index] = vectors[index]
    elif vector_indexes and generate_embeddings:
        generated = embed_texts(
            [f"{clean_payloads[index].title}\n{clean_payloads[index].content}" for index in vector_indexes],
            settings.embedding_dimensions,
        )
        if len(generated) != len(vector_indexes):
            raise ValueError("embedding count does not match vectorized document payload count")
        for index, embedding in zip(vector_indexes, generated):
            embeddings[index] = embedding
    if sync_vector_store and any(embeddings[index] is None for index in vector_indexes):
        raise ValueError("RAG documents require embeddings before vector store synchronization")
    provided_vectors = [embeddings[index] for index in vector_indexes if embeddings[index] is not None]
    if provided_vectors:
        _validate_embeddings(provided_vectors)

    docs: list[Document] = []
    for payload, embedding in zip(clean_payloads, embeddings):
        vectorized = embedding is not None
        embedding_metadata = (
            {
                "embedding_provider": embedding_provider_name(),
                "embedding_model": settings.embedding_model,
                "embedding_dimensions": settings.embedding_dimensions,
                **(embedding_info or {}),
            }
            if vectorized
            else {}
        )
        docs.append(
            Document(
                repo_id=_as_uuid(repo_id),
                source_type=payload.source_type,
                source_id=_as_uuid(payload.source_id),
                title=payload.title[:500],
                content=payload.content,
                embedding=embedding,
                meta={
                    **(payload.metadata or {}),
                    **policy_metadata(payload.source_type),
                    **embedding_metadata,
                },
            )
        )
    if not docs:
        return []
    db.add_all(docs)
    db.flush()
    if commit:
        db.commit()
        for doc in docs:
            db.refresh(doc)
    return docs


def replace_documents(
    db: Session,
    repo_id: str | uuid.UUID,
    source_types: list[str],
    payloads: list[DocumentPayload],
    *,
    commit: bool = True,
    vectors: list[list[float]] | None = None,
    embedding_info: dict[str, Any] | None = None,
    generate_embeddings: bool = True,
    sync_vector_store: bool = True,
) -> list[Document]:
    repo_uuid = _as_uuid(repo_id)
    # 向量存在 documents 行内：删行即删向量，无需单独清理向量库。
    db.query(Document).filter(Document.repo_id == repo_uuid, Document.source_type.in_(source_types)).delete(
        synchronize_session=False
    )
    db.flush()
    return add_documents(
        db,
        repo_uuid,
        payloads,
        commit=commit,
        vectors=vectors,
        embedding_info=embedding_info,
        generate_embeddings=generate_embeddings,
        sync_vector_store=sync_vector_store,
    )


def replace_documents_by_metadata(
    db: Session,
    repo_id: str | uuid.UUID,
    source_type: str,
    metadata_filters: dict[str, Any],
    payloads: list[DocumentPayload],
    *,
    commit: bool = True,
) -> list[Document]:
    repo_uuid = _as_uuid(repo_id)
    rows = db.query(Document).filter(Document.repo_id == repo_uuid, Document.source_type == source_type).all()
    document_ids = [
        row.id
        for row in rows
        if all((row.meta or {}).get(key) == value for key, value in metadata_filters.items())
    ]
    if document_ids:
        db.query(Document).filter(Document.id.in_(document_ids)).delete(synchronize_session=False)
    db.flush()
    docs = add_documents(db, repo_uuid, payloads, commit=commit)
    return docs


def delete_document_group(
    db: Session,
    repo_id: str | uuid.UUID,
    source_id: str | uuid.UUID,
    source_type: str | None = None,
    *,
    commit: bool = True,
) -> int:
    repo_uuid = _as_uuid(repo_id)
    source_uuid = _as_uuid(source_id)
    query = db.query(Document).filter(Document.repo_id == repo_uuid, Document.source_id == source_uuid)
    if source_type:
        query = query.filter(Document.source_type == source_type)
    count = query.delete(synchronize_session=False)
    if commit:
        db.commit()
    return count
