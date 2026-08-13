"""告警 + 监控测试。"""
from datetime import datetime, timedelta, timezone

import pytest

from app.alerting import send_diagnosis_alert
from app.config import settings
from app.db import write_session
from app.models import AgentRun, ToolCallLog


def test_alert_disabled_noop(monkeypatch):
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "")
    sent = send_diagnosis_alert({"run_id": "x", "report": {"conclusion": "c"}})
    assert sent is False


def test_alert_posts_signed_payload(monkeypatch):
    captured = {}

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

    def fake_post(url, *, content, headers, timeout):
        captured["url"] = url
        captured["body"] = content
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr(settings, "ALERT_WEBHOOK_URL", "https://hook.test/alert")
    monkeypatch.setattr(settings, "ALERT_WEBHOOK_SECRET", "s3cret")
    monkeypatch.setattr("app.alerting.httpx.post", fake_post)

    sent = send_diagnosis_alert({
        "run_id": "abc", "item_id": 1, "status": "ok", "stop_reason": "final",
        "report": {"conclusion": "成交归零", "suggestions": ["检查库存"]},
    })
    assert sent is True
    assert captured["url"] == "https://hook.test/alert"
    assert "X-Alert-Signature" in captured["headers"]
    body = captured["body"].decode("utf-8")
    assert "abc" in body
    assert "成交归零" in body


def test_monitoring_aggregates():
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with write_session() as s:
        # 清空共享测试库里的历史运行记录，保证总数断言确定
        s.query(AgentRun).delete()
        s.query(ToolCallLog).delete()
        s.add(AgentRun(run_id="m1", item_id=1, status="succeeded", steps=3, tool_calls=2,
                       tokens_in=100, tokens_out=50, llm_calls=3, llm_duration_ms=900, duration_ms=1000,
                       created_at=now))
        s.add(AgentRun(run_id="m2", item_id=2, status="error", steps=1, tool_calls=0,
                       tokens_in=10, tokens_out=5, llm_calls=1, llm_duration_ms=300, duration_ms=300,
                       error="llm timeout", created_at=now))
        s.add(ToolCallLog(run_id="m1", step=1, tool="metric", args_json="{}", result_summary="",
                          rows=3, latency_ms=50.0, status="ok", created_at=now))
        s.add(ToolCallLog(run_id="m1", step=2, tool="funnel", args_json="{}", result_summary="",
                          rows=3, latency_ms=150.0, status="error", created_at=now))
        s.commit()

    from app.monitoring import collect_monitoring

    m = collect_monitoring(window_hours=24)
    assert m["agent_runs"]["total"] == 2
    assert m["agent_runs"]["error_rate"] == 0.5
    assert m["agent_runs"]["avg_llm_latency_ms"] == 300.0  # (900/3 + 300/1)/2
    assert m["tool_calls"]["total"] == 2
    assert m["tool_calls"]["by_tool"] == {"metric": 1, "funnel": 1}
    assert m["tool_calls"]["error_rate"] == 0.5
    assert m["tool_calls"]["avg_latency_ms"] == 100.0
