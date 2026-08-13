"""FastAPI 入口：REST API + 健康检查。

所有写操作只走服务层；Agent 是纯内部组件，外部只能通过 API 提交任务、查询结果。
"""
from __future__ import annotations

from fastapi import FastAPI, Request

from app.api import diagnostics, files, health, monitoring, tasks
from app.db import init_db

app = FastAPI(
    title="电商商品经营异常诊断 Agent",
    description="SQL/Python 发现问题 → Agent 调查问题 → LLM 决策总结 → Tool 取证",
    version="0.1.0",
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """给所有响应加安全头（API 型项目最该加的几项）。"""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"      # 防 MIME 类型混淆
    response.headers["X-Frame-Options"] = "DENY"                # 防点击劫持（禁止 iframe 嵌入）
    response.headers["Referrer-Policy"] = "no-referrer"         # 防 URL 信息经 Referer 泄露
    response.headers["Cache-Control"] = "no-store"              # 敏感接口禁止缓存
    response.headers["Content-Security-Policy"] = "default-src 'none'"  # 纯 JSON API 不加载任何资源
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.include_router(health.router)
app.include_router(diagnostics.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")
app.include_router(monitoring.router, prefix="/api/v1")


@app.on_event("startup")
def _startup() -> None:
    init_db()
