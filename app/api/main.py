"""FastAPI 入口：REST API + 健康检查。

所有写操作只走服务层；Agent 是纯内部组件，外部只能通过 API 提交任务、查询结果。
"""
from __future__ import annotations

from fastapi import FastAPI

from app.api import diagnostics, files, health, tasks
from app.db import init_db

app = FastAPI(
    title="电商商品经营异常诊断 Agent",
    description="SQL/Python 发现问题 → Agent 调查问题 → LLM 决策总结 → Tool 取证",
    version="0.1.0",
)

app.include_router(health.router)
app.include_router(diagnostics.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(files.router, prefix="/api/v1")


@app.on_event("startup")
def _startup() -> None:
    init_db()
