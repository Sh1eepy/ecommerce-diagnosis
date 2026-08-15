"""监控面板扩展：历史趋势 / 慢查询 / token 成本 / 反馈聚合。

与 app/monitoring.py（单点快照）互补：这里提供时间序列与文件侧聚合。

设计取舍：
- 分桶在 Python 内完成，不写 MySQL/SQLite 方言 SQL —— 规避方言差异坑
  （README 第 9 条踩坑：SQLite 的 SUM 返回 int、日期返回字符串），
  且监控数据量小（每日几百行），Python 分桶毫秒级。
- 慢查询扫描只读 ≤5MB 的 jsonl（跳过 cli.jsonl 这类历史累积大文件），
  按行内 ts（本地时间）过滤窗口。
- DB 内 created_at 为 naive UTC，分桶前需显式按 UTC 解释再转时间戳。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import settings
from app.db import read_session
from app.models import AgentRun, AnomalyEvent, DiagnosticReport, Task, ToolCallLog

# 慢查询扫描：跳过超大日志文件（cli.jsonl 历史累积可达 100MB+）
_SQL_LOG_MAX_BYTES = 5 * 1024 * 1024


def _utcnow_naive() -> datetime:
    """与 models.utcnow() 一致的 naive UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bucket_ts(dt: datetime, bucket_seconds: int) -> int:
    """naive UTC datetime → 桶起点时间戳（UTC 解释，避免被当成本地时间）。"""
    ts = dt.replace(tzinfo=timezone.utc).timestamp()
    return int(ts) // bucket_seconds * bucket_seconds


def collect_history(window_hours: int = 24, bucket_minutes: int = 60) -> dict:
    """按时间桶聚合 AgentRun / ToolCallLog / Task，返回连续时间序列。

    - 所有桶都会被填充（含空桶），保证图表连续。
    - 返回的 bucket_start 为 UTC ISO 字符串，前端 new Date() 自动转本地展示。
    """
    if bucket_minutes < 1:
        bucket_minutes = 60
    bucket_seconds = bucket_minutes * 60
    now = _utcnow_naive()
    since = now - timedelta(hours=window_hours)

    with read_session() as s:
        runs = s.query(AgentRun).filter(AgentRun.created_at >= since).all()
        tools = s.query(ToolCallLog).filter(ToolCallLog.created_at >= since).all()
        task_rows = s.query(Task).filter(Task.created_at >= since).all()

    # 桶起点序列（窗口起点对齐）
    first = _bucket_ts(since, bucket_seconds)
    last = _bucket_ts(now, bucket_seconds)
    buckets: dict[int, dict] = {}
    t = first
    while t <= last:
        buckets[t] = {
            "agent_runs": {"total": 0, "error": 0, "duration_sum": 0.0,
                           "llm_calls_sum": 0, "llm_duration_sum": 0.0,
                           "tokens_in": 0, "tokens_out": 0},
            "tool_calls": {"total": 0, "error": 0, "latency_sum": 0.0},
            "tasks": {},
        }
        t += bucket_seconds

    for r in runs:
        b = buckets.setdefault(_bucket_ts(r.created_at, bucket_seconds), _empty_bucket())
        a = b["agent_runs"]
        a["total"] += 1
        a["error"] += 1 if r.status == "error" else 0
        a["duration_sum"] += r.duration_ms or 0
        a["llm_calls_sum"] += r.llm_calls or 0
        a["llm_duration_sum"] += r.llm_duration_ms or 0
        a["tokens_in"] += r.tokens_in or 0
        a["tokens_out"] += r.tokens_out or 0

    for tlog in tools:
        b = buckets.setdefault(_bucket_ts(tlog.created_at, bucket_seconds), _empty_bucket())
        c = b["tool_calls"]
        c["total"] += 1
        c["error"] += 1 if tlog.status == "error" else 0
        c["latency_sum"] += tlog.latency_ms or 0

    for tk in task_rows:
        b = buckets.setdefault(_bucket_ts(tk.created_at, bucket_seconds), _empty_bucket())
        b["tasks"][tk.status] = b["tasks"].get(tk.status, 0) + 1

    series = []
    for start_ts in sorted(buckets):
        b = buckets[start_ts]
        a = b["agent_runs"]
        c = b["tool_calls"]
        series.append({
            "bucket_start": datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat(),
            "agent_runs": {
                "total": a["total"],
                "error": a["error"],
                "error_rate": round(a["error"] / a["total"], 4) if a["total"] else 0.0,
                "avg_duration_ms": round(a["duration_sum"] / a["total"], 1) if a["total"] else None,
                "avg_llm_latency_ms": round(a["llm_duration_sum"] / a["llm_calls_sum"], 1)
                if a["llm_calls_sum"] else None,
                "tokens_in": a["tokens_in"],
                "tokens_out": a["tokens_out"],
            },
            "tool_calls": {
                "total": c["total"],
                "error": c["error"],
                "error_rate": round(c["error"] / c["total"], 4) if c["total"] else 0.0,
                "avg_latency_ms": round(c["latency_sum"] / c["total"], 1) if c["total"] else None,
            },
            "tasks": b["tasks"],
        })
    return {"window_hours": window_hours, "bucket_minutes": bucket_minutes,
            "buckets": series}


def _empty_bucket() -> dict:
    return {
        "agent_runs": {"total": 0, "error": 0, "duration_sum": 0.0,
                       "llm_calls_sum": 0, "llm_duration_sum": 0.0,
                       "tokens_in": 0, "tokens_out": 0},
        "tool_calls": {"total": 0, "error": 0, "latency_sum": 0.0},
        "tasks": {},
    }


def slow_queries(window_hours: int = 24, min_ms: float = 1000.0, limit: int = 20) -> list[dict]:
    """扫描 logs/sql_logs/ 找出慢 SQL（duration_ms ≥ min_ms）。

    注意：sql_logs 的 ts 是 tracing._ts() 写入的**本地时间**（非 UTC），
    因此窗口过滤用本地时间；DB 侧的时间统一为 UTC。
    """
    since_local = datetime.now() - timedelta(hours=window_hours)
    log_dir = Path(settings.LOG_DIR) / "sql_logs"
    if not log_dir.exists():
        return []

    hits: list[dict] = []
    for f in log_dir.glob("*.jsonl"):
        try:
            if f.stat().st_size > _SQL_LOG_MAX_BYTES:
                continue
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts_raw = obj.get("ts", "")
                dur = obj.get("duration_ms") or 0
                if not ts_raw or dur < min_ms:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_raw)
                except ValueError:
                    continue
                if ts < since_local:
                    continue
                hits.append({
                    "ts": ts_raw,
                    "run_id": obj.get("run_id", ""),
                    "statement": obj.get("statement", "")[:200],
                    "duration_ms": round(float(dur), 1),
                    "rows": obj.get("rows", 0),
                })
        except OSError:
            continue

    hits.sort(key=lambda h: h["duration_ms"], reverse=True)
    return hits[:limit]


def estimate_cost(tokens_in: int, tokens_out: int) -> float:
    """按配置单价估算 LLM token 成本（元）。"""
    cost = (
        tokens_in / 1_000_000 * settings.LLM_INPUT_PRICE_PER_M
        + tokens_out / 1_000_000 * settings.LLM_OUTPUT_PRICE_PER_M
    )
    return round(cost, 4)


def collect_feedback() -> dict:
    """聚合 feedback/agent_feedback/*.json 的用户反馈（评分/类别分布）。"""
    base = Path(settings.LOG_DIR).parent / "feedback" / "agent_feedback"
    if not base.exists():
        return {"total": 0, "avg_rating": None, "by_category": {}, "rating_histogram": {}}

    records: list[dict] = []
    for f in base.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            records.extend(data.get("records", []))
        except (json.JSONDecodeError, OSError):
            continue

    if not records:
        return {"total": 0, "avg_rating": None, "by_category": {}, "rating_histogram": {}}

    ratings = [r["rating"] for r in records if isinstance(r.get("rating"), int)]
    by_category: dict[str, int] = {}
    for r in records:
        cat = r.get("category", "other")
        by_category[cat] = by_category.get(cat, 0) + 1
    histogram: dict[str, int] = {}
    for v in ratings:
        histogram[str(v)] = histogram.get(str(v), 0) + 1
    return {
        "total": len(records),
        "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None,
        "by_category": by_category,
        "rating_histogram": histogram,
    }


def collect_anomalies(
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    scope: str = "all",
    diagnosed_only: bool = False,
) -> dict:
    """异常事件列表（anomaly_event 表，按检出时间倒序，带分页与筛选）。

    scope: all=全部, item=仅商品级(item_id!=0), category=仅类目级(item_id==0)
    diagnosed_only: 只看已有诊断报告的异常
    返回 {"anomalies": [...], "total": 筛选后总数}
    """
    with read_session() as s:
        q = s.query(AnomalyEvent)
        if status:
            q = q.filter(AnomalyEvent.status == status)
        if scope == "item":
            q = q.filter(AnomalyEvent.item_id != 0)
        elif scope == "category":
            q = q.filter(AnomalyEvent.item_id == 0)
        # 一次查出已诊断的 anomaly_id 集合
        reported_ids = {r[0] for r in s.query(DiagnosticReport.anomaly_id).all()
                        if r[0] is not None}
        if diagnosed_only:
            q = q.filter(AnomalyEvent.id.in_(reported_ids))
        total = q.count()
        # 已诊断的排最前（有报告的优先展示），其余按检出时间倒序
        q = q.order_by(
            AnomalyEvent.id.in_(reported_ids).desc(),
            AnomalyEvent.detected_at.desc(),
        )
        rows = q.offset(offset).limit(limit).all()

    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "item_id": r.item_id,
            "category_id": r.category_id,
            "metric": r.metric,
            "rule_id": r.rule_id,
            "rule_name": r.rule_name,
            "date_start": r.date_start.isoformat() if r.date_start else "",
            "date_end": r.date_end.isoformat() if r.date_end else "",
            "baseline_value": r.baseline_value,
            "current_value": r.current_value,
            "change_pct": round(r.change_pct, 1),
            "severity": r.severity,
            "status": r.status,
            "description": r.description,
            "detected_at": r.detected_at.strftime("%Y-%m-%d %H:%M") if r.detected_at else "",
            "has_report": r.id in reported_ids,
        })
    return {"anomalies": out, "total": total}


def get_report_for_anomaly(anomaly_id: int) -> dict | None:
    """按 anomaly_id 查最近一次诊断报告（diagnostic_report 表）。"""
    with read_session() as s:
        rep = (
            s.query(DiagnosticReport)
            .filter(DiagnosticReport.anomaly_id == anomaly_id)
            .order_by(DiagnosticReport.created_at.desc())
            .first()
        )
        if rep is None:
            return None
        try:
            content = json.loads(rep.content_json) if rep.content_json else {}
        except json.JSONDecodeError:
            content = {}
        return {
            "run_id": rep.run_id,
            "model": rep.model,
            "item_id": rep.item_id,
            "created_at": rep.created_at.strftime("%Y-%m-%d %H:%M") if rep.created_at else "",
            "report": content,
        }


def alert_status() -> dict:
    """告警 Webhook 配置状态（只暴露域名，不泄露 URL 参数/密钥）。"""
    url = (settings.ALERT_WEBHOOK_URL or "").strip()
    host = ""
    if url:
        try:
            from urllib.parse import urlparse
            host = urlparse(url).netloc
        except ValueError:
            host = "已配置"
    return {"configured": bool(url), "host": host}
