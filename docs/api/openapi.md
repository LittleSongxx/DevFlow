# DevFlow AI API

> 本文档只列出常用端点子集。**完整的交互式 OpenAPI 以 `http://localhost:8000/docs` 为准**——
> 后端共有约 100 个端点（含 RAG、知识库、工作流、评测、Webhook 等模块）。
> 若后端启用了 `DEVFLOW_API_KEY`，除 `/health`、`/docs` 与 webhook 外的所有请求都需要携带 `X-API-Key` 请求头。

## Core Endpoints

- `POST /api/repos/connect`
- `GET /api/repos`
- `POST /api/repos/{repo_id}/sync`
- `GET /api/repos/{repo_id}/sync-status`
- `GET /api/repos/{repo_id}/issues`
- `POST /api/issues/{issue_id}/analyze`
- `GET /api/issues/{issue_id}/similar`
- `GET /api/repos/{repo_id}/pull-requests`
- `POST /api/pull-requests/{pr_id}/analyze`
- `POST /api/pull-requests/{pr_id}/review-checklist`
- `GET /api/repos/{repo_id}/workflow-runs`
- `GET /api/workflow-runs/{run_id}/failed-jobs`
- `POST /api/workflow-runs/{run_id}/analyze`
- `POST /api/chat`
- `POST /api/search`
- `POST /api/reports/weekly`
- `POST /api/evals/run`
