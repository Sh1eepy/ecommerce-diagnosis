"""诊断接口：提交一次异常诊断（同步或异步进队列）。"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.agent.agent import Agent
from app.config import settings
from app.db import read_session
from app.metrics.compute import _check_range
from app.models import AnomalyEvent
from app.security import require_scope
from app.tasks.queue import create_task

router = APIRouter(tags=["diagnostics"])


class DiagnosticRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: int = Field(gt=0, le=2**63 - 1, strict=True, description="商品 ID")
    start_date: date = Field(description="分析窗口开始 YYYY-MM-DD")
    end_date: date = Field(description="分析窗口结束 YYYY-MM-DD")
    anomaly_id: int | None = Field(default=None, gt=0, le=2**31 - 1, strict=True, description="关联的异常事件 ID（可选）")
    sync: bool = Field(default=False, description="true=同步执行并返回报告（调试用）；false=进任务队列")

    @model_validator(mode="after")
    def valid_window(self):
        _check_range(self.start_date, self.end_date)
        return self


def _anomaly_text(anomaly_id: int | None, item_id: int) -> str:
    if not anomaly_id:
        return ""
    with read_session() as s:
        a = s.get(AnomalyEvent, anomaly_id)
    if not a:
        raise HTTPException(status_code=404, detail="异常事件不存在")
    if a.item_id != item_id:
        raise HTTPException(status_code=422, detail="异常事件与诊断商品不匹配")
    return f"{a.metric}: {a.description}（严重度 {a.severity}）"


@router.post("/diagnostics", status_code=201)
def create_diagnostic(req: DiagnosticRequest, _: str = Depends(require_scope("diagnosis:create"))) -> dict:
    if req.sync and settings.APP_ENV == "production":
        raise HTTPException(status_code=403, detail="生产环境仅支持异步诊断")
    anomaly = _anomaly_text(req.anomaly_id, req.item_id)
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
