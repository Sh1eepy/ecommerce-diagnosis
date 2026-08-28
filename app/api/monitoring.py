"""监控指标接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.monitoring import collect_monitoring
from app.security import require_scope

router = APIRouter(tags=["monitoring"])


@router.get("/monitoring")
def monitoring(window_hours: int = 24, _: str = Depends(require_scope("report:read"))) -> dict:
    """聚合最近 N 小时的运行时指标（LLM 延迟/错误率、工具耗时、任务积压等）。"""
    if window_hours < 1 or window_hours > 24 * 30:
        window_hours = 24
    return collect_monitoring(window_hours)
