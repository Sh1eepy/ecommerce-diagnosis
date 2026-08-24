"""证据账本与报告质量门槛测试。"""
from app.agent.context import truncate_context
from app.agent.quality import evaluate_report
from app.agent.workflow import Workflow


def _evidence():
    return {
        "metric#1": {
            "data": {"summary": {"current": {"cvr": 4.0}}},
            "tool": "metric", "rows": 1, "summary": "cvr=4.0",
        }
    }


def _report(value=4.0):
    return {
        "facts": [{
            "point": "支付转化率为4", "value": value,
            "evidence_ref": {"call_id": "metric#1", "path": "summary.current.cvr"},
        }],
        "hypotheses": [{
            "id": "H1", "statement": "成交环节转化下降", "status": "supported",
            "confidence": 0.8, "evidence_refs": ["metric#1"],
        }],
        "analysis": {"primary_hypothesis_id": "H1"},
        "conclusion": "成交环节转化下降",
        "suggestions": [{
            "action": "排查支付链路", "rationale": "验证成交损失", "owner": "交易研发",
            "priority": "P0", "success_metric": "cvr 恢复至基线",
        }],
    }


def test_quality_accepts_grounded_report():
    result = evaluate_report(_report(), _evidence())
    assert result["passed"] is True
    assert all(score == 1.0 for score in result["scores"].values())


def test_quality_rejects_invented_number():
    result = evaluate_report(_report(value=7.0), _evidence())
    assert result["passed"] is False
    assert result["scores"]["numeric_consistency"] == 0.0


def test_quality_rejects_narrative_that_claims_nonzero_metric_is_zero():
    report = _report()
    report["facts"][0]["metric"] = "cvr"
    report["conclusion"] = "支付转化率已经归零"
    result = evaluate_report(report, _evidence())
    assert result["passed"] is False
    assert result["scores"]["narrative_consistency"] == 0.0


def test_quality_accepts_equal_non_numeric_evidence_value():
    report = _report()
    report["facts"].append({
        "point": "不可用日期",
        "value": ["2015-08-30", "2015-09-06"],
        "evidence_ref": {"call_id": "metric#1", "path": "unavailable_dates"},
    })
    evidence = _evidence()
    evidence["metric#1"]["data"]["unavailable_dates"] = ["2015-08-30", "2015-09-06"]
    assert evaluate_report(report, evidence)["passed"] is True


def test_failed_tool_does_not_complete_workflow():
    workflow = Workflow()
    workflow.observe("metric", {"ok": False})
    assert workflow.can_finalize() is False


def test_context_truncation_keeps_recent_evidence():
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "user", "content": "old" * 20},
        {"role": "user", "content": "LATEST_EVIDENCE"},
    ]
    kept = truncate_context(messages, max_chars=30)
    assert any("LATEST_EVIDENCE" in m["content"] for m in kept)
    assert not any(m["content"] == "old" * 20 for m in kept)
