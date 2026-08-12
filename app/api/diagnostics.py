"""诊断接口：提交一次异常诊断（同步或异步进队列）。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.agent.agent import Agent
from app.db import read_session
from app.models import AnomalyEvent
from app.security import verify_api_key
from app.tasks.queue import create_task

router = APIRouter(tags=["diagnostics"])


class DiagnosticRequest(BaseModel):
    item_id: int = Field(gt=0, description="商品 ID")
    start_date: date = Field(description="分析窗口开始 YYYY-MM-DD")
    end_date: date = Field(description="分析窗口结束 YYYY-MM-DD")
    anomaly_id: int | None = Field(default=None, description="关联的异常事件 ID（可选）")
    sync: bool = Field(default=False, description="true=同步执行并返回报告（调试用）；false=进任务队列")


def _anomaly_text(anomaly_id: int | None) -> str:
    if not anomaly_id:
        return ""
    with read_session() as s:
        a = s.get(AnomalyEvent, anomaly_id)
    if not a:
        return ""
    return f"{a.metric}: {a.description}（严重度 {a.severity}）"


@router.post("/diagnostics", status_code=201)
def create_diagnostic(req: DiagnosticRequest, _: str = Depends(verify_api_key)) -> dict:
    anomaly = _anomaly_text(req.anomaly_id)
    payload = {
        "item_id": req.item_id,
        "start_date": str(req.start_date),
        "end_date": str(req.end_date),
        "anomaly": anomaly,
    }

    if req.sync:
        # 同步模式：直接跑一次 Agent（调试/演示用）
        result = Agent().run(
            req.item_id, req.start_date, req.end_date,
            anomaly=anomaly, anomaly_id=req.anomaly_id,
        )
        return {"sync": True, **result}

    # 异步模式：幂等入队
    idem = f"diag:{req.item_id}:{req.start_date}:{req.end_date}:{req.anomaly_id or ''}"
    task = create_task("diagnose", payload, anomaly_id=req.anomaly_id, idempotency_key=idem)
    return {"sync": False, "task_id": task.id, "status": task.status}
