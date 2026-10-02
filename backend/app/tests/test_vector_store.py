import uuid

import pytest

from app.core.config import settings
from app.db.models import Document
from app.services.rag.embeddings import embed_knowledge_texts, validate_embedding_contract
from app.services.rag.vector_store import (
    VectorStoreUnavailableError,
    _validate_embeddings,
    _vector_literal,
    vector_store_status,
)


def test_vector_literal_encodes_pgvector_text_form() -> None:
    assert _vector_literal([1.0, 0.0, -0.5]) == "[1.0,0.0,-0.5]"


def test_validate_embeddings_rejects_wrong_dimensions(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_dimensions", 4)

    with pytest.raises(ValueError, match="Refusing to truncate or pad"):
        _validate_embeddings([[1.0, 0.0]])


def test_validate_embeddings_rejects_mixed_batch(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_dimensions", 2)

    with pytest.raises(ValueError, match="Refusing to truncate or pad"):
        _validate_embeddings([[1.0, 0.0], [1.0, 0.0, 0.0]])


def test_vector_search_disabled_raises_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vector_search_enabled", False)

    with pytest.raises(VectorStoreUnavailableError, match="disabled"):
        from app.services.rag.vector_store import search_documents_by_vector

        search_documents_by_vector(uuid.uuid4(), "anything")


def test_vector_store_status_reports_degraded_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "vector_search_enabled", False)

    status = vector_store_status()

    assert status["backend"] == "pgvector"
    assert status["status"] == "degraded"
    assert status["available"] is False
    assert "disabled" in status["error"]


def test_embedding_contract_rejects_per_repository_vector_spaces() -> None:
    with pytest.raises(ValueError, match="Embedding contract mismatch"):
        validate_embedding_contract("deterministic", "other-model", 384)


def test_explicit_deterministic_provider_is_allowed_without_becoming_a_fallback(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_provider", "deterministic")
    monkeypatch.setattr(settings, "embedding_model", "deterministic-local")
    monkeypatch.setattr(settings, "embedding_dimensions", 8)

    vectors = embed_knowledge_texts(
        ["token failure"],
        provider="deterministic",
        model="deterministic-local",
        dimensions=8,
    )

    assert len(vectors) == 1
    assert len(vectors[0]) == 8


def test_embedding_provider_failure_is_not_replaced_with_hash_vectors(monkeypatch) -> None:
    monkeypatch.setattr(settings, "embedding_provider", "openai_compatible")
    monkeypatch.setattr(settings, "embedding_model", "configured-model")
    monkeypatch.setattr(settings, "embedding_dimensions", 8)
    monkeypatch.setattr(
        "app.services.rag.embeddings._embed_openai_compatible",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("provider offline")),
    )

    with pytest.raises(RuntimeError, match="provider offline"):
        embed_knowledge_texts(
            ["token failure"],
            provider="openai_compatible",
            model="configured-model",
            dimensions=8,
        )


def test_document_model_accepts_embedding_on_sqlite(tmp_path) -> None:
    """SQLite 方言下向量列退化为 JSON 文本存储，写入不报错。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'vs.db'}")
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(bind=engine)
    repo_id = uuid.uuid4()
    with session_factory() as db:
        from app.db.models import Repository

        db.add(Repository(id=repo_id, owner="o", name="n", full_name="o/n"))
        doc = Document(
            repo_id=repo_id,
            source_type="issue",
            source_id=uuid.uuid4(),
            title="T",
            content="C",
            meta={},
            embedding=[0.1, 0.2, 0.3],
        )
        db.add(doc)
        db.commit()
        stored = db.query(Document).one()
        assert stored.embedding == [0.1, 0.2, 0.3]
