"""运行时监控指标：从 agent_run / tool_call_log / task / anomaly_event 聚合。

对应上线排查场景"Agent 突然变慢"：LLM 平均延迟/错误率、工具平均耗时、任务积压量。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db import read_session
from app.models import AgentRun, AnomalyEvent, Task, ToolCallLog


def _avg(values) -> float | None:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def collect_monitoring(window_hours: int = 24) -> dict:
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=window_hours)
    with read_session() as s:
        runs = s.query(AgentRun).filter(AgentRun.created_at >= since).all()
        tools = s.query(ToolCallLog).filter(ToolCallLog.created_at >= since).all()
        task_rows = s.query(Task).filter(Task.created_at >= since).all()
        open_anomalies = s.query(AnomalyEvent).filter_by(status="open").count()

    run_err = [r for r in runs if r.status in {"error", "failed"}]
    run_status: dict[str, int] = {}
    for r in runs:
        run_status[r.status] = run_status.get(r.status, 0) + 1
    tool_err = [t for t in tools if t.status == "error"]

    tool_counts: dict[str, int] = {}
    for t in tools:
        tool_counts[t.tool] = tool_counts.get(t.tool, 0) + 1

    task_status: dict[str, int] = {}
    for t in task_rows:
        task_status[t.status] = task_status.get(t.status, 0) + 1

    return {
        "window_hours": window_hours,
        "agent_runs": {
            "total": len(runs),
            "succeeded": run_status.get("succeeded", 0),
            "incomplete": run_status.get("incomplete", 0),
            "retrying": run_status.get("retrying", 0),
            "running": run_status.get("running", 0),
            "by_status": run_status,
            "error": len(run_err),
            "error_rate": round(len(run_err) / len(runs), 3) if runs else 0.0,
            "avg_duration_ms": _avg([r.duration_ms for r in runs]),
            "avg_llm_calls": _avg([r.llm_calls for r in runs]),
            "avg_llm_latency_ms": _avg([r.llm_duration_ms / r.llm_calls for r in runs if r.llm_calls]),
            "total_tokens_in": sum(r.tokens_in for r in runs),
            "total_tokens_out": sum(r.tokens_out for r in runs),
        },
        "tool_calls": {
            "total": len(tools),
            "by_tool": tool_counts,
            "error": len(tool_err),
            "error_rate": round(len(tool_err) / len(tools), 3) if tools else 0.0,
            "avg_latency_ms": _avg([t.latency_ms for t in tools]),
        },
        "tasks": {
            **task_status,
            "pending_backlog": task_status.get("pending", 0) + task_status.get("retrying", 0),
        },
        "anomalies": {"open": open_anomalies},
    }
