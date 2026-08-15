"""监控面板扩展测试：历史趋势分桶 / 慢查询扫描 / token 成本 / 反馈聚合 / 路由。

依赖 conftest 的 SQLite 临时库 + LOG_DIR 重定向，全离线。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.db import write_session
from app.models import AgentRun, AnomalyEvent, DiagnosticReport, Task, ToolCallLog
from app.monitoring_history import (
    _bucket_ts,
    alert_status,
    collect_anomalies,
    collect_feedback,
    collect_history,
    estimate_cost,
    get_report_for_anomaly,
    slow_queries,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bucket_start_iso(dt: datetime) -> str:
    """naive UTC datetime → 所在桶的 bucket_start（与 collect_history 输出同格式）。"""
    return datetime.fromtimestamp(_bucket_ts(dt, 3600), tz=timezone.utc).isoformat()


@pytest.fixture()
def seed_monitoring():
    """造监控数据：3 个 run 落在 3 个不同的小时桶（间隔 1.4h / 1.5h），便于精确断言。"""
    now = _utcnow()
    runs = [
        # (run_id, status, delta_h, llm_calls, llm_duration_ms, duration_ms, tin, tout)
        ("r-a", "succeeded", 0.1, 3, 4500, 12000, 1000, 300),
        ("r-b", "error", 1.5, 1, 800, 4000, 400, 150),
        ("r-c", "succeeded", 3.0, 2, 2000, 6000, 500, 100),
    ]
    with write_session() as s:
        # 清空监控表：隔离其他测试（test_agent_loop/test_api 会写 agent_run 等表）
        s.query(AgentRun).delete()
        s.query(ToolCallLog).delete()
        s.query(Task).delete()
        for rid, status, dh, calls, llm_dur, dur, tin, tout in runs:
            s.add(AgentRun(
                run_id=rid, item_id=1, status=status,
                steps=4, tool_calls=2, tokens_in=tin, tokens_out=tout,
                llm_calls=calls, llm_duration_ms=llm_dur, duration_ms=dur,
                created_at=now - timedelta(hours=dh),
            ))
        s.add(ToolCallLog(run_id="r-a", step=1, tool="metric", latency_ms=120.0, status="ok",
                          created_at=now - timedelta(hours=0.1)))
        s.add(ToolCallLog(run_id="r-b", step=2, tool="funnel", latency_ms=2500.0, status="error",
                          created_at=now - timedelta(hours=1.5)))
        s.add(Task(idempotency_key="t1", status="pending", created_at=now - timedelta(hours=0.1)))
        s.commit()
    yield


def _find_bucket(hist: dict, bucket_start_iso: str) -> dict:
    for b in hist["buckets"]:
        if b["bucket_start"] == bucket_start_iso:
            return b
    raise AssertionError(f"桶不存在: {bucket_start_iso}")


def test_collect_history_buckets(seed_monitoring):
    now = _utcnow()
    hist = collect_history(window_hours=24, bucket_minutes=60)
    buckets = hist["buckets"]
    # 24h 窗口按整点对齐：24 或 25 个连续桶
    assert 24 <= len(buckets) <= 25
    starts = [b["bucket_start"] for b in buckets]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)

    # --- 桶 A：r-a（0.1h 前，成功）---
    ba = _find_bucket(hist, _bucket_start_iso(now - timedelta(hours=0.1)))
    assert ba["agent_runs"]["total"] == 1
    assert ba["agent_runs"]["error"] == 0
    assert ba["agent_runs"]["error_rate"] == 0.0
    assert ba["agent_runs"]["tokens_in"] == 1000
    assert ba["agent_runs"]["tokens_out"] == 300
    assert ba["agent_runs"]["avg_llm_latency_ms"] == pytest.approx(1500.0)  # 4500/3
    assert ba["agent_runs"]["avg_duration_ms"] == pytest.approx(12000.0)
    assert ba["tool_calls"]["total"] == 1
    assert ba["tool_calls"]["error"] == 0
    assert ba["tool_calls"]["avg_latency_ms"] == pytest.approx(120.0)
    assert ba["tasks"].get("pending") == 1

    # --- 桶 B：r-b（1.5h 前，失败）---
    bb = _find_bucket(hist, _bucket_start_iso(now - timedelta(hours=1.5)))
    assert bb["agent_runs"]["total"] == 1
    assert bb["agent_runs"]["error"] == 1
    assert bb["agent_runs"]["error_rate"] == 1.0
    assert bb["agent_runs"]["avg_llm_latency_ms"] == pytest.approx(800.0)
    assert bb["tool_calls"]["total"] == 1
    assert bb["tool_calls"]["error"] == 1
    assert bb["tool_calls"]["avg_latency_ms"] == pytest.approx(2500.0)

    # --- 桶 C：r-c（3h 前）---
    bc = _find_bucket(hist, _bucket_start_iso(now - timedelta(hours=3.0)))
    assert bc["agent_runs"]["total"] == 1
    assert bc["agent_runs"]["tokens_in"] == 500

    # --- 窗口汇总 ---
    assert sum(b["agent_runs"]["total"] for b in buckets) == 3
    assert sum(b["agent_runs"]["error"] for b in buckets) == 1
    assert sum(b["tool_calls"]["total"] for b in buckets) == 2
    assert sum(b["tasks"].get("pending", 0) for b in buckets) == 1


def _runtime_dir(sub: str) -> Path:
    """项目内临时目录（pytest 的 tmp_path 在系统 Temp，沙箱环境下不可写）。"""
    d = Path(__file__).resolve().parent / ".pytest_runtime" / sub
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_slow_queries(monkeypatch):
    """只返回窗口内且耗时达标、跳过超大文件。"""
    from app.config import settings

    log_dir = _runtime_dir("tmp_slow") / "sql_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    now_local = datetime.now()
    ts = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]  # noqa: E731

    (log_dir / "normal.jsonl").write_text(
        "\n".join([
            json.dumps({"ts": ts(now_local), "run_id": "x1", "statement": "SELECT slow", "duration_ms": 2000, "rows": 5}),
            json.dumps({"ts": ts(now_local), "run_id": "x2", "statement": "SELECT fast", "duration_ms": 100, "rows": 1}),
            json.dumps({"ts": ts(now_local - timedelta(hours=48)), "run_id": "x3", "statement": "SELECT old", "duration_ms": 5000, "rows": 9}),
            "not-json",
        ]),
        encoding="utf-8",
    )
    (log_dir / "huge.jsonl").write_text("x" * (5 * 1024 * 1024 + 1), encoding="utf-8")  # 超大文件应被跳过

    monkeypatch.setattr(settings, "LOG_DIR", str(_runtime_dir("tmp_slow")))
    result = slow_queries(window_hours=24, min_ms=1000.0, limit=20)
    assert len(result) == 1
    assert result[0]["run_id"] == "x1"
    assert result[0]["duration_ms"] == 2000.0


def test_estimate_cost():
    # 默认单价 in=2元/M, out=8元/M：1M in + 0.5M out = 2 + 4 = 6
    assert estimate_cost(1_000_000, 500_000) == pytest.approx(6.0)
    assert estimate_cost(0, 0) == 0.0


def test_collect_feedback(monkeypatch):
    from app.config import settings

    base = _runtime_dir("tmp_fb")
    fb_dir = base / "feedback" / "agent_feedback"
    fb_dir.mkdir(parents=True, exist_ok=True)
    (fb_dir / "r1.json").write_text(json.dumps({
        "records": [
            {"run_id": "r1", "rating": 5, "category": "data", "comment": "", "ts": "2026-08-13"},
            {"run_id": "r1", "rating": 3, "category": "analysis", "comment": "ok", "ts": "2026-08-13"},
        ]
    }), encoding="utf-8")

    monkeypatch.setattr(settings, "LOG_DIR", str(base / "logs"))
    fb = collect_feedback()
    assert fb["total"] == 2
    assert fb["avg_rating"] == pytest.approx(4.0)
    assert fb["by_category"] == {"data": 1, "analysis": 1}
    assert fb["rating_histogram"]["5"] == 1


def test_dashboard_page_ok():
    """页面可达且是 HTML（CSP 已单独放行）。"""
    client = TestClient(app)
    res = client.get("/api/v1/monitoring/dashboard")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "echarts" in res.text
    assert "监控面板" in res.text


def test_history_api_requires_key():
    client = TestClient(app)
    # 无 Key → 401
    assert client.get("/api/v1/monitoring/history").status_code in (401, 403)
    # 有 Key → 200
    res = client.get("/api/v1/monitoring/history", headers={"X-API-Key": "test-key"})
    assert res.status_code == 200
    assert "buckets" in res.json()


def test_slow_queries_api_ok():
    client = TestClient(app)
    res = client.get("/api/v1/monitoring/slow-queries", headers={"X-API-Key": "test-key"})
    assert res.status_code == 200
    assert "queries" in res.json()


# ================= 异常列表 / 报告 / 告警 =================

@pytest.fixture()
def seed_anomaly_report():
    """造 1 条异常 + 1 份诊断报告（隔离其他测试写入的数据）。"""
    with write_session() as s:
        s.query(AnomalyEvent).delete()
        s.query(DiagnosticReport).delete()
        anom = AnomalyEvent(
            item_id=355908, metric="cvr", rule_id="consecutive_decline",
            rule_name="连续下降", date_start=date(2015, 6, 1),
            date_end=date(2015, 6, 14),
            baseline_value=7.0, current_value=4.0, change_pct=-42.9,
            severity="high", status="open", description="由 7.0 连续 3 天降至 4.0",
        )
        s.add(anom)
        s.flush()
        s.add(DiagnosticReport(
            run_id="rep-abc", anomaly_id=anom.id, item_id=355908,
            content_json='{"facts": [{"point": "转化率下降", "evidence": "7.0→4.0"}],'
                         '"analysis": {"key_finding": "漏斗加购环节流失"},'
                         '"conclusion": "商品自身问题", "suggestions": ["检查价格"]}',
            model="deepseek-chat",
        ))
        s.commit()
        yield anom.id


def test_collect_anomalies_marks_report(seed_anomaly_report):
    data = collect_anomalies(status="open", limit=50)
    anomalies = data["anomalies"]
    assert len(anomalies) == 1
    assert data["total"] == 1
    a = anomalies[0]
    assert a["has_report"] is True
    assert a["change_pct"] == -42.9
    assert a["severity"] == "high"
    assert a["metric"] == "cvr"


def test_collect_anomalies_scope_and_diagnosed(seed_anomaly_report):
    # 商品级筛选能命中
    data = collect_anomalies(status="open", scope="item", limit=50)
    assert data["total"] == 1
    # 类目级筛选命中 0 条（seed 的是商品级）
    assert collect_anomalies(status="open", scope="category", limit=50)["total"] == 0
    # 仅已诊断能命中
    d2 = collect_anomalies(status="open", scope="item", diagnosed_only=True, limit=50)
    assert d2["total"] == 1
    assert d2["anomalies"][0]["has_report"] is True
    # 分页：offset 越界返回空
    assert collect_anomalies(status="open", limit=50, offset=100)["anomalies"] == []


def test_get_report_for_anomaly(seed_anomaly_report):
    rep = get_report_for_anomaly(seed_anomaly_report)
    assert rep is not None
    assert rep["run_id"] == "rep-abc"
    assert rep["report"]["conclusion"] == "商品自身问题"
    assert rep["report"]["suggestions"] == ["检查价格"]
    # 不存在的异常 → None
    assert get_report_for_anomaly(999999) is None


def test_anomalies_and_report_api(seed_anomaly_report):
    client = TestClient(app)
    res = client.get("/api/v1/monitoring/anomalies", headers={"X-API-Key": "test-key"})
    assert res.status_code == 200
    body = res.json()
    assert len(body["anomalies"]) == 1
    assert body["anomalies"][0]["has_report"] is True

    res2 = client.get(f"/api/v1/monitoring/reports/{body['anomalies'][0]['id']}",
                      headers={"X-API-Key": "test-key"})
    assert res2.status_code == 200
    assert res2.json()["report"]["conclusion"] == "商品自身问题"

    # 未诊断的异常 → 404
    assert client.get("/api/v1/monitoring/reports/999999",
                      headers={"X-API-Key": "test-key"}).status_code == 404


def test_alert_status(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "")
    assert alert_status() == {"configured": False, "host": ""}

    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://oapi.dingtalk.com/robot/send?access_token=x")
    st = alert_status()
    assert st["configured"] is True
    assert st["host"] == "oapi.dingtalk.com"  # 不泄露 query 参数
