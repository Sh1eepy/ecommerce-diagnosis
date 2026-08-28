"""FastAPI 入口：REST API + 健康检查。

所有写操作只走服务层；Agent 是纯内部组件，外部只能通过 API 提交任务、查询结果。
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from app.api import diagnostics, files, health, monitoring, monitoring_extra, tasks
from app.db import init_db
from app.security import RequestBodyLimitMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动成功后才接收请求；初始化失败直接阻止服务启动。
    init_db()
    yield


app = FastAPI(
    title="电商商品经营异常诊断 Agent",
    description="SQL/Python 发现问题 → Agent 调查问题 → LLM 决策总结 → Tool 取证",
    version="0.1.0",
    lifespan=lifespan,
)
app.add_middleware(RequestBodyLimitMiddleware)

# 监控面板页面：需要加载 CDN 的 ECharts 与内联 JS，CSP 单独放行（其余 API 保持严格）
_DASHBOARD_CSP = (
    "default-src 'self'; "
    "script-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data:; font-src https://cdn.jsdelivr.net; "
    "connect-src 'self'"
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """给所有响应加安全头（API 型项目最该加的几项）。"""
    response = await call_next(request)
    if request.url.path == "/api/v1/monitoring/dashboard":
        response.headers["Content-Security-Policy"] = _DASHBOARD_CSP
    else:
        response.headers["Content-Security-Policy"] = "default-src 'none'"  # 纯 JSON API 不加载任何资源
    response.headers["X-Content-Type-Options"] = "nosniff"      # 防 MIME 类型混淆
    response.headers["X-Frame-Options"] = "DENY"                # 防点击劫持（禁止 iframe 嵌入）
    response.headers["Referrer-Policy"] = "no-referrer"         # 防 URL 信息经 Referer 泄露
    response.headers["Cache-Control"] = "no-store"              # 敏感接口禁止缓存
    if request.url.scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


app.include_router(health.router)
app.include_router(diagnostics.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")
app.include_router(monitoring.router, prefix="/api/v1")
app.include_router(monitoring_extra.router, prefix="/api/v1")
