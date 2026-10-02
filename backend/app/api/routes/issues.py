import asyncio
import json
from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.db.models import AnalysisResult, Conversation, Document, Issue, Repository
from app.db.session import get_db
from app.schemas.issues import IssueAnalysisResponse, IssueResponse
from app.services.agents.issue_agent import IssueAgent
from app.api.routes._analysis_common import (
    PROJECT_DOC_TYPES,
    code_documents,
    conversation_evidence,
    document_evidence as _document_evidence,
    encode_sse as _encode_sse,
    fallback_project_documents as _fallback_project_documents,
    semantic_documents,
    team_member_profiles,
    trace_item as _trace_item,
)
from app.core.redaction import sanitize_error

router = APIRouter()
PUBLIC_REASONING_PROMPT = """
你是 DevFlow AI 的 Issue Agent。请用中文流式输出一段“公开分析过程”，帮助用户理解你如何分析当前 Issue。
要求：
- 只输出面向用户的可审计分析摘要，不输出隐藏链路思维或私有推理链。
- 简洁说明你检查了哪些证据、如何判断结论/优先级/复杂度/负责人。
- 不要编造输入中没有的文件、人员、Issue 或 PR。
- 不要输出最终 JSON。
"""




def _issue_payload(issue: Issue) -> dict:
    return {
        "id": str(issue.id),
        "title": issue.title,
        "body": issue.body,
        "labels": issue.labels,
        "number": issue.number,
        "state": issue.state,
        "assignees": issue.assignees,
        "author": issue.author,
    }


async def _build_issue_analysis_context(db: Session, issue: Issue, conversation_id: UUID | None = None) -> tuple[str, dict, dict]:
    query = f"{issue.title}\n{issue.body or ''}"
    repo = db.get(Repository, issue.repo_id)
    similar_docs = await semantic_documents(db, str(issue.repo_id), query, "issue", 4)
    duplicates = [
        {
            "issue_id": item["source_id"],
            "title": item["title"],
            "score": max(0.0, min(1.0, float(item["score"]))),
        }
        for item in similar_docs
        if item["source_id"] != str(issue.id)
    ]
    code_docs = _document_evidence(
        await code_documents(repo, query, limit=4),
        "code",
    )
    project_docs = _document_evidence(
        await semantic_documents(
            db,
            str(issue.repo_id),
            query,
            limit=4,
            source_types=sorted(PROJECT_DOC_TYPES),
        ),
        "document",
    )
    if not project_docs:
        project_docs = _fallback_project_documents(db, issue.repo_id)
    chat_evidence = await conversation_evidence(db, issue.repo_id, conversation_id, query)
    team_members = team_member_profiles(db, issue.repo_id)
    context = {
        "repo_id": str(issue.repo_id),
        "duplicate_candidates": duplicates,
        "code_references": code_docs,
        "project_documents": project_docs,
        "conversation_evidence": chat_evidence,
        "team_members": team_members,
    }
    counts = {
        "duplicates": len(duplicates),
        "code": len(code_docs),
        "documents": len(project_docs),
        "conversation": len(chat_evidence),
        "team_members": len(team_members),
    }
    return query, context, counts


def _persist_issue_analysis(
    db: Session,
    issue: Issue,
    result: dict,
    model_name: str,
    conversation_id: UUID | None,
    evidence_counts: dict,
) -> AnalysisResult:
    analysis = AnalysisResult(
        target_type="issue",
        target_id=issue.id,
        analysis_type="issue_analysis",
        input_snapshot={
            "issue_id": str(issue.id),
            "conversation_id": str(conversation_id) if conversation_id else None,
            "evidence_counts": evidence_counts,
        },
        result_json=result,
        model_name=model_name,
    )
    db.add(analysis)
    db.commit()
    db.refresh(analysis)
    return analysis




def _public_reasoning_fallback(issue: Issue, context: dict) -> list[str]:
    duplicates = context.get("duplicate_candidates") or []
    code_refs = context.get("code_references") or []
    docs = context.get("project_documents") or []
    members = context.get("team_members") or []
    return [
        f"- 我先读取 Issue #{issue.number} 的标题、正文、标签和当前状态，确认它描述的是「{issue.title}」。\n",
        f"- 然后检索相似历史 Issue，找到 {len(duplicates)} 个候选；如果相似度很高，会优先建议合并到已有 Issue。\n",
        f"- 接着查看代码和项目文档证据：代码线索 {len(code_refs)} 条，文档/记忆线索 {len(docs)} 条，用于判断影响范围和复杂度。\n",
        f"- 最后结合团队画像，当前可参考成员 {len(members)} 人；负责人只会从已配置成员或明确方向中选择。\n",
        "- 这些公开分析步骤会汇总成结构化结论：结论、优先级、复杂度、负责人、证据、checklist 和可选草稿。\n",
    ]


async def _stream_public_reasoning(agent: IssueAgent, issue: Issue, payload: dict, context: dict) -> AsyncIterator[str]:
    streamed = False
    user_payload = {
        "issue": payload,
        "evidence_counts": {
            "duplicates": len(context.get("duplicate_candidates") or []),
            "code_references": len(context.get("code_references") or []),
            "project_documents": len(context.get("project_documents") or []),
            "conversation_evidence": len(context.get("conversation_evidence") or []),
            "team_members": len(context.get("team_members") or []),
        },
        "context_preview": {
            "duplicates": context.get("duplicate_candidates", [])[:3],
            "code_references": context.get("code_references", [])[:3],
            "project_documents": context.get("project_documents", [])[:3],
            "team_members": context.get("team_members", [])[:3],
        },
    }
    async for chunk in agent.llm.chat_text_stream(
        PUBLIC_REASONING_PROMPT,
        [{"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)}],
    ):
        streamed = True
        yield chunk
    if streamed:
        return
    for line in _public_reasoning_fallback(issue, context):
        await asyncio.sleep(0.06)
        yield line


async def _issue_analysis_sse(issue_id: UUID, conversation_id: UUID | None, db: Session) -> AsyncIterator[str]:
    issue = db.get(Issue, issue_id)
    if issue is None:
        yield _encode_sse("error", {"message": "未找到 Issue"})
        return
    agent = IssueAgent()
    payload = _issue_payload(issue)
    try:
        yield _encode_sse("trace", _trace_item("step", "读取 Issue", f"读取 Issue #{issue.number} 的标题、正文、标签、状态和已分配人员。", "running"))
        query, context, counts = await _build_issue_analysis_context(db, issue, conversation_id)
        yield _encode_sse("trace", _trace_item("tool", "检索相似历史", f"找到 {counts['duplicates']} 个相似 Issue 候选。"))
        yield _encode_sse("trace", _trace_item("tool", "检索代码与文档", f"找到 {counts['code']} 条代码线索、{counts['documents']} 条项目文档/记忆线索。"))
        yield _encode_sse("trace", _trace_item("tool", "读取团队画像", f"读取 {counts['team_members']} 位团队成员，用于建议负责人。"))
        if counts["conversation"]:
            yield _encode_sse("trace", _trace_item("tool", "读取会话上下文", f"找到 {counts['conversation']} 条与当前会话相关的证据。"))
        yield _encode_sse("trace", _trace_item("thinking", "LLM 公开分析过程", "开始综合 Issue、证据和团队画像。", "running"))
        async for chunk in _stream_public_reasoning(agent, issue, payload, context):
            yield _encode_sse("thinking_delta", {"content": chunk})
        result = await agent.run(payload, context)
        analysis = _persist_issue_analysis(db, issue, result, agent.llm.model, conversation_id, counts)
        yield _encode_sse("trace", _trace_item("result", "生成结构化结论", f"结论：{result.get('conclusion')}；优先级：{result.get('priority')}；复杂度：{result.get('complexity')}；负责人：{result.get('suggested_owner')}。"))
        yield _encode_sse("final", {"analysis_id": str(analysis.id), "result": result})
    except Exception as exc:
        db.rollback()
        yield _encode_sse("error", {"message": f"分析失败：{sanitize_error(exc)}"})


@router.get("/repos/{repo_id}/issues", response_model=list[IssueResponse])
async def list_issues(
    repo_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> list[Issue]:
    return db.query(Issue).filter(Issue.repo_id == repo_id).order_by(Issue.number.desc()).offset(offset).limit(limit).all()


@router.get("/issues/{issue_id}", response_model=IssueResponse)
async def get_issue(issue_id: UUID, db: Session = Depends(get_db)) -> Issue:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="未找到 Issue")
    return issue


@router.post("/issues/{issue_id}/analyze", response_model=IssueAnalysisResponse)
async def analyze_issue(
    issue_id: UUID,
    conversation_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> IssueAnalysisResponse:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="未找到 Issue")
    agent = IssueAgent()
    _query, context, counts = await _build_issue_analysis_context(db, issue, conversation_id)
    result = await agent.run(_issue_payload(issue), context)
    analysis = _persist_issue_analysis(db, issue, result, agent.llm.model, conversation_id, counts)
    return IssueAnalysisResponse(analysis_id=analysis.id, result=result)


@router.post("/issues/{issue_id}/analyze/stream")
async def analyze_issue_stream(
    issue_id: UUID,
    conversation_id: UUID | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    return StreamingResponse(_issue_analysis_sse(issue_id, conversation_id, db), media_type="text/event-stream")


@router.get("/issues/{issue_id}/similar")
async def similar_issues(issue_id: UUID, db: Session = Depends(get_db)) -> list[dict]:
    issue = db.get(Issue, issue_id)
    if issue is None:
        raise HTTPException(status_code=404, detail="未找到 Issue")
    return await semantic_documents(db, str(issue.repo_id), f"{issue.title}\n{issue.body or ''}", "issue", 5)
