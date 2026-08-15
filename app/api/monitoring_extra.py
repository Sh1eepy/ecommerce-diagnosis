"""监控面板扩展路由：历史趋势 / 慢查询 / 反馈 / 成本 / Dashboard 页面。

数据接口全部复用 verify_api_key（与 /monitoring 一致）；
Dashboard 页面本身是静态壳（HTML+JS），数据获取仍需 API Key。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app.db import read_session
from app.models import AgentRun
from app.monitoring_history import (
    alert_status,
    collect_anomalies,
    collect_feedback,
    collect_history,
    estimate_cost,
    get_report_for_anomaly,
    slow_queries,
)
from app.security import verify_api_key

router = APIRouter(tags=["monitoring"])

_WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


@router.get("/monitoring/history")
def monitoring_history(
    hours: int = 24,
    bucket: int = 60,
    _: str = Depends(verify_api_key),
) -> dict:
    """按时间桶聚合的历史趋势（运行量/错误率/LLM 延迟/token/任务积压）。"""
    if hours < 1 or hours > 24 * 30:
        hours = 24
    return collect_history(window_hours=hours, bucket_minutes=bucket)


@router.get("/monitoring/slow-queries")
def monitoring_slow_queries(
    hours: int = 24,
    min_ms: float = 1000.0,
    limit: int = 20,
    _: str = Depends(verify_api_key),
) -> dict:
    """扫描 sql_logs 找出慢 SQL（含语句/耗时/run_id）。"""
    if hours < 1 or hours > 24 * 30:
        hours = 24
    if limit < 1 or limit > 100:
        limit = 20
    return {
        "window_hours": hours,
        "min_ms": min_ms,
        "queries": slow_queries(hours, min_ms, limit),
    }


@router.get("/monitoring/feedback")
def monitoring_feedback(_: str = Depends(verify_api_key)) -> dict:
    """用户反馈聚合（平均分/类别分布/评分直方图）。"""
    return collect_feedback()


@router.get("/monitoring/cost")
def monitoring_cost(hours: int = 24, _: str = Depends(verify_api_key)) -> dict:
    """最近 N 小时 LLM token 成本估算（元）。"""
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=hours)
    with read_session() as s:
        rows = s.query(AgentRun).filter(AgentRun.created_at >= since).all()
    tin = sum(r.tokens_in or 0 for r in rows)
    tout = sum(r.tokens_out or 0 for r in rows)
    return {
        "window_hours": hours,
        "runs": len(rows),
        "tokens_in": tin,
        "tokens_out": tout,
        "cost_cny": estimate_cost(tin, tout),
    }


@router.get("/monitoring/alerts")
def monitoring_alerts(_: str = Depends(verify_api_key)) -> dict:
    """告警 Webhook 配置状态（只暴露域名）。"""
    return alert_status()


@router.get("/monitoring/anomalies")
def monitoring_anomalies(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    scope: str = "all",
    diagnosed_only: bool = False,
    _: str = Depends(verify_api_key),
) -> dict:
    """异常事件列表（分页 + 商品/类目筛选 + 仅看已诊断）。"""
    if limit < 1 or limit > 200:
        limit = 50
    if offset < 0:
        offset = 0
    if scope not in ("all", "item", "category"):
        scope = "all"
    return collect_anomalies(
        status=status or None,
        limit=limit,
        offset=offset,
        scope=scope,
        diagnosed_only=diagnosed_only,
    )


@router.get("/monitoring/reports/{anomaly_id}")
def monitoring_report(anomaly_id: int, _: str = Depends(verify_api_key)) -> dict:
    """按异常 ID 查诊断报告；未诊断返回 404。"""
    rep = get_report_for_anomaly(anomaly_id)
    if rep is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="该异常尚未生成诊断报告")
    return rep


@router.get("/monitoring/dashboard")
def monitoring_dashboard() -> FileResponse:
    """监控面板页面（静态 HTML；数据接口仍需 X-API-Key）。"""
    return FileResponse(_WEB_DIR / "dashboard.html", media_type="text/html")
