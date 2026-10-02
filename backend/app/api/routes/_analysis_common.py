"""Issue / PR / CI 分析路由共享的工具函数。

原先在三个路由文件中复制粘贴；收敛到一处，并把阻塞调用
（pgvector/BM25 检索、ripgrep 代码搜索、上下文组装）放到线程池，
避免在 async 路由里阻塞事件循环。
"""

import asyncio
import json
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.models import Conversation, Document, Repository
from app.services.chat_memory import ContextAssembler
from app.services.code_search import search_repository_code
from app.db.session import to_thread_serialized
from app.services.rag.retrieval import search_similar_documents_async

PROJECT_DOC_TYPES = {"knowledge_file", "memory_note", "project_doc"}


def encode_sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def trace_item(kind: str, title: str, content: str, status: str = "done") -> dict:
    return {"kind": kind, "title": title, "content": content, "status": status}


def document_evidence(items: list[dict], default_source_type: str) -> list[dict]:
    evidence = []
    for item in items:
        snippet = str(item.get("snippet") or "").strip()
        title = str(item.get("title") or "").strip()
        if not snippet or not title:
            continue
        evidence.append(
            {
                "source_type": item.get("source_type") or default_source_type,
                "title": title,
                "snippet": snippet,
                "source_id": str(item.get("source_id") or item.get("id") or ""),
                "score": item.get("score"),
                "metadata": item.get("metadata") or {},
            }
        )
    return evidence


def dedupe_evidence(items: list[dict], limit: int) -> list[dict]:
    seen: set[str] = set()
    output: list[dict] = []
    for item in items:
        key = f"{item.get('source_type')}:{item.get('source_id')}:{str(item.get('title'))[:80]}"
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
        if len(output) >= limit:
            break
    return output


def fallback_project_documents(db: Session, repo_id: UUID) -> list[dict]:
    rows = (
        db.query(Document)
        .filter(Document.repo_id == repo_id, Document.source_type.in_(PROJECT_DOC_TYPES))
        .order_by(Document.created_at.desc())
        .limit(4)
        .all()
    )
    return [
        {
            "source_type": doc.source_type,
            "title": doc.title,
            "snippet": doc.content[:260],
            "source_id": str(doc.source_id),
            "score": None,
            "metadata": doc.meta or {},
        }
        for doc in rows
    ]


async def semantic_documents(db: Session, repo_id: UUID | str, query: str, *args, **kwargs) -> list[dict]:
    return await search_similar_documents_async(db, repo_id, query, *args, **kwargs)


async def code_documents(repo: Repository | None, query: str, *, limit: int = 4, **kwargs) -> list[dict]:
    if repo is None:
        return []
    return await asyncio.to_thread(search_repository_code, repo, query, limit=limit, **kwargs)


async def conversation_evidence(db: Session, repo_id: UUID, conversation_id: UUID | None, query: str) -> list[dict]:
    if not conversation_id:
        return []
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.repo_id != repo_id:
        return []
    repo = db.get(Repository, repo_id)
    assembled = await to_thread_serialized(db, ContextAssembler(db).assemble, repo, conversation, query, limit=8)
    evidence = document_evidence(assembled.evidence, "conversation")
    if assembled.system_context.strip():
        evidence.append(
            {
                "source_type": "conversation",
                "title": f"会话上下文：{conversation.title}",
                "snippet": assembled.system_context[:320],
                "source_id": str(conversation.id),
                "score": None,
            }
        )
    return evidence[:5]


def team_member_profiles(db: Session, repo_id: UUID) -> list[dict]:
    return [
        {
            "name": (doc.meta or {}).get("name") or doc.title.replace("团队成员：", "").replace("Team member: ", ""),
            "role": (doc.meta or {}).get("role") or "",
            "strengths": (doc.meta or {}).get("strengths") or "",
            "techStack": (doc.meta or {}).get("tech_stack") or "",
        }
        for doc in db.query(Document).filter(Document.repo_id == repo_id, Document.source_type == "team_member").all()
    ]
