"""证据账本与报告质量门槛测试。"""
import copy

import pytest

from app.agent.context import truncate_context
from app.agent.quality import evaluate_report
from app.agent.workflow import Workflow


def _evidence():
    return {
        "metric#1": {
            "call_id": "metric#1",
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
            "id": "H1", "statement": "成交环节可能变化，尚需基线核实", "status": "uncertain",
            "confidence": 0.5, "evidence_refs": ["metric#1"],
        }],
        "analysis": {"attribution_status": "uncertain", "primary_hypothesis_id": None,
                     "limitations": ["只有当前指标，缺少基线和业务核查，尚不能确认原因"]},
        "conclusion": "成交环节是否变化需基线核实",
        "suggestions": [{
            "action": "排查支付链路", "rationale": "验证成交损失", "owner": "交易研发",
            "priority": "P0", "success_metric": "cvr 恢复至基线",
        }],
    }


def test_quality_accepts_grounded_report():
    result = evaluate_report(_report(), _evidence())
    assert result["passed"] is True
    assert all(score == 1.0 for score in result["scores"].values())


def test_human_memory_is_not_current_numeric_evidence():
    report = _report()
    report["facts"][0]["evidence_ref"] = {"call_id": "review-123", "path": "correct_lesson"}
    assert not evaluate_report(report, _evidence())["passed"]


def test_small_sample_limits_and_unverified_significance():
    evidence = _evidence()
    evidence["metric#1"]["data"]["summary"]["sample_counts"] = {
        "current": {"transaction_count": 0}, "previous": {"transaction_count": 1}}
    report = _report()
    report["conclusion"] = "转化率显著低于同行"
    result = evaluate_report(report, evidence)
    assert not result["passed"]
    assert "small_sample" in result["evidence_limits"]
    assert any("significance_unverified" in e for e in result["errors"])


@pytest.mark.parametrize("section", ["change", "focus"])
def test_quality_accepts_fact_sections_without_changing_evidence_rules(section):
    report = _report()
    report["facts"][0]["section"] = section
    assert evaluate_report(report, _evidence())["passed"] is True
    report["facts"][0]["value"] = 999
    assert evaluate_report(report, _evidence())["passed"] is False


@pytest.mark.parametrize("field,value", [("section", "cause"), ("section", []), ("point", ""), ("point", {})])
def test_quality_rejects_malformed_fact_presentation(field, value):
    report = _report()
    report["facts"][0][field] = value
    assert evaluate_report(report, _evidence())["passed"] is False


@pytest.mark.parametrize("field,value", [("priority", "urgent"), ("owner", []), ("rationale", {}), ("success_metric", 1)])
def test_action_details_are_typed_and_priority_is_bounded(field, value):
    report = _report()
    report["suggestions"][0][field] = value
    assert evaluate_report(report, _evidence())["passed"] is False


def test_both_windows_are_required_to_remove_coverage_limit():
    evidence = _evidence()
    full = {"expected_days": 7, "observed_days": 7, "dates_without_rows": []}
    evidence["metric#1"]["data"]["summary"]["coverage"] = {"current": full, "previous": full}
    assert "daily_coverage_unverified" not in evaluate_report(_report(), evidence)["evidence_limits"]
    evidence["metric#1"]["data"]["summary"]["coverage"]["previous"] = {
        "expected_days": 7, "observed_days": 6, "dates_without_rows": ["2015-05-31"]}
    assert "daily_coverage_unverified" in evaluate_report(_report(), evidence)["evidence_limits"]


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


@pytest.mark.parametrize("claim,accepted", [
    ("当前窗口访客数增加但成交为0，CVR降至0%。", True),
    ("访客数增加，成交为0。", True),
    ("UV增加成交为0。", True),
    ("访客数增加。GMV为0。", True),
    ("访客为0.5。", True),  # 不是零值断言；该规则不负责一般数字核对。
    ("访客数已经归零。", False),
    ("访客数为0。", False),
    ("UV和成交均为0。", False),
    ("访客数与成交都为0。", False),
    ("成交增加但访客数为0。", False),
])
def test_zero_claim_stays_with_its_metric_subject(claim, accepted):
    report, evidence = _report(), _evidence()
    evidence["metric#1"]["data"]["summary"]["current"]["uv"] = 24
    report["facts"].append({"point": "访客数24", "metric": "uv", "value": 24,
                            "evidence_ref": {"call_id": "metric#1", "path": "summary.current.uv"}})
    report["analysis"]["key_finding"] = claim
    assert evaluate_report(report, evidence)["passed"] is accepted


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


def test_quality_accepts_uncertainty_without_inventing_a_hypothesis():
    report = _report()
    report["hypotheses"] = []
    result = evaluate_report(report, _evidence())
    assert result["passed"] is True
    assert result["attribution_status"] == "uncertain"
    assert "causal_unverified" in result["evidence_limits"]


@pytest.mark.parametrize("status", ["supported", "rejected", "invented", ["uncertain"]])
def test_model_cannot_certify_or_rule_out_a_cause(status):
    report = _report()
    report["hypotheses"][0]["status"] = status
    assert evaluate_report(report, _evidence())["passed"] is False


@pytest.mark.parametrize("limitations", [None, [], "仅供参考", [""], [None]])
def test_uncertainty_requires_explicit_limitations(limitations):
    report = _report()
    report["analysis"]["limitations"] = limitations
    assert evaluate_report(report, _evidence())["passed"] is False


def test_primary_cause_and_model_supplied_proof_do_not_bypass_guard():
    report = _report()
    report["analysis"].update(primary_hypothesis_id="H1", attribution_status="confirmed", causal_verified=True)
    assert evaluate_report(report, _evidence())["passed"] is False


@pytest.mark.parametrize("claim", [
    "类目大盘正常，排除大盘影响。",
    "类目整体下跌是主要原因。",
    "可能需要检查商品。大盘正常，排除大盘影响。",
    "商品不可售导致无成交。",
    "事实表明商品在窗口内不可售。",
    "异常根因为商品下架。",
    "GMV损失约73万。",
    "预计GMV损失约73万元。",
    "The market is normal.",
    "The item was unavailable and caused the decline.",
    "库存可能变化但已经确认导致无成交。",
    "库存变化可能导致无成交。商品不可售导致无成交。",
    "Revenue loss was 731040.",
])
def test_unsupported_narrative_is_rejected_even_with_uncertain_status(claim):
    report = _report()
    report["conclusion"] = claim
    result = evaluate_report(report, _evidence())
    assert result["passed"] is False
    assert any(e.startswith("证据越界") for e in result["errors"])


@pytest.mark.parametrize("claim", [
    "不能确认大盘正常，不能排除大盘影响。",
    "商品不可用记录需要核实，尚不能确认不可售导致成交变化。",
    "可能因商品下架导致变化，仍需业务核查。",
    "价格或库存变化可能导致无成交，仍需业务核查。",
    "库存变化或许造成成交下降，尚未确认。",
    "不能将GMV差额视为损失，缺少实际损失核算依据。",
    "GMV估算指标较上期减少731040，具体原因尚未确认。",
    "Cannot rule out market effects.",
])
def test_qualified_claims_and_observed_differences_are_allowed(claim):
    report = _report()
    report["conclusion"] = claim
    assert evaluate_report(report, _evidence())["passed"] is True


@pytest.mark.parametrize("field", ["fact", "hypothesis", "impact", "rationale"])
def test_overclaim_cannot_hide_outside_conclusion(field):
    report = _report()
    target, key = {
        "fact": (report["facts"][0], "point"),
        "hypothesis": (report["hypotheses"][0], "statement"),
        "impact": (report["analysis"], "impact"),
        "rationale": (report["suggestions"][0], "rationale"),
    }[field]
    target[key] = "类目大盘正常"
    assert evaluate_report(report, _evidence())["passed"] is False


def test_limits_come_from_evidence_including_legacy_checkpoints():
    evidence = _evidence()
    evidence["metric#1"]["data"].update(unavailable_dates=["2015-08-30"], price=91380)
    evidence["metric#1"]["data"]["summary"]["current"]["gmv"] = 0
    evidence["peer#2"] = {"tool": "peer", "data": {"category_total": {"gmv": 782340}}}
    before = copy.deepcopy(evidence)
    result = evaluate_report(_report(), evidence)
    assert result["passed"] is True
    assert set(result["evidence_limits"]) == {
        "causal_unverified", "daily_coverage_unverified", "availability_observations_only",
        "category_baseline_missing", "gmv_proxy_not_loss",
    }
    assert evidence == before


@pytest.mark.parametrize("ref", [[], "metric#1", {"call_id": []}, {"call_id": "metric#1", "path": ""}])
def test_malformed_fact_refs_fail_closed(ref):
    report = _report()
    report["facts"][0]["evidence_ref"] = ref
    assert evaluate_report(report, _evidence())["passed"] is False


def test_duplicate_hypothesis_ids_and_bad_refs_are_rejected():
    report = _report()
    report["hypotheses"].append(copy.deepcopy(report["hypotheses"][0]))
    report["hypotheses"][1]["evidence_refs"] = [{}]
    result = evaluate_report(report, _evidence())
    assert result["passed"] is False


def test_investigation_does_not_treat_model_confidence_as_causal_proof():
    from app.agent.investigation import InvestigationState

    state = InvestigationState.from_dict({"evidence": _evidence(), "hypotheses": [{
        "id": "H1", "statement": "可能由商品配置变化引起", "status": "supported",
        "confidence": 0.99, "evidence_refs": ["metric#1"],
    }]})
    assert state.snapshot()["hypotheses"][0]["status"] == "uncertain"
    assert "causal_unverified" in state.snapshot()["evidence_limits"]


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
