"""Agent Loop 测试（MockLLM 全离线）。"""
import copy
import json
from datetime import date

import pytest

from app.agent.agent import Agent
from app.agent.tool import ToolRegistry
from app.llm.mock import MockLLM


def _plan_agent(plan):
    return Agent(llm=MockLLM(plan=plan))


def _hypothesis(hid="H1", refs=None, status="active", confidence=0.5):
    return {
        "id": hid, "statement": "成交环节效率下降", "confidence": confidence,
        "status": status, "evidence_refs": refs or [],
    }


def _valid_report(call_id="metric#1", path="summary.current.uv", value=14000):
    return {
        "facts": [{
            "point": f"证据值为 {value}", "metric": "uv", "value": value, "unit": "raw",
            "evidence_ref": {"call_id": call_id, "path": path},
        }],
        "hypotheses": [_hypothesis(refs=[call_id], status="uncertain", confidence=0.5)],
        "analysis": {
            "attribution_status": "uncertain", "primary_hypothesis_id": None,
            "key_finding": "已取得观察值，成交环节效率变化尚需基线确认",
            "impact": "影响成交", "limitations": ["只能确认相关关系"],
        },
        "conclusion": "成交环节效率下降是待验证假设，尚不能确认原因",
        "suggestions": [{
            "action": "检查成交链路", "rationale": "验证主假设", "owner": "商品运营",
            "priority": "P1", "success_metric": "成交率恢复至基线",
        }],
    }


def test_agent_happy_path():
    plan = [
        {"type": "tool_call", "reasoning": "先看历史趋势", "hypothesis": _hypothesis(),
         "tool": "metric", "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "tool_call", "reasoning": "定位漏斗环节", "hypothesis": _hypothesis(refs=["metric#1"]),
         "tool": "funnel", "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "tool_call", "reasoning": "维度拆解", "hypothesis": _hypothesis(refs=["metric#1", "funnel#2"]),
         "tool": "dimension", "args": {"item_id": 1, "dimension": "new_user", "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "final", "report": _valid_report("funnel#2", "stages.0.count", 42000)},
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok"
    assert result["stop_reason"] == "final"
    assert result["tool_calls"] == 3
    assert result["quality"]["passed"] is True
    assert result["report"]["suggestions"]


def test_budget_preflight_does_not_send_unaffordable_request(monkeypatch):
    from app.config import settings
    from app.llm.base import LLMClient
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 100)

    class RealEstimateMock(MockLLM):
        estimate_input_tokens = LLMClient.estimate_input_tokens

        def chat(self, messages, **kwargs):
            pytest.fail("预算不足时不得请求模型")

    result = Agent(llm=RealEstimateMock()).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "incomplete" and result["stop_reason"] == "token_budget"
    assert result["tool_calls"] == 0


def test_budget_reserves_correction_and_accepts_exact_budget_valid_final(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 60)
    bad = _valid_report()
    bad["conclusion"] = "同行CVR正常"

    class Recording(MockLLM):
        requests = []

        def chat(self, messages, **kwargs):
            self.requests.append((copy.deepcopy(messages), kwargs["max_tokens"]))
            return super().chat(messages, **kwargs)

    llm = Recording(plan=[
        {"type": "tool_call", "tool": "metric", "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "final", "report": bad}, {"type": "final", "report": _valid_report()},
    ])
    result = Agent(llm=llm).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok"
    assert [limit for _, limit in llm.requests] == [50, 30, 10]
    assert any("final 未通过" in m["content"] for m in llm.requests[-1][0])
    assert any("[预算收尾]" in m["content"] for m in llm.requests[-1][0])


def test_low_budget_cannot_execute_more_tools_if_model_ignores_final_request(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 60)
    call = {"type": "tool_call", "tool": "metric", "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}}
    result = Agent(llm=MockLLM(plan=[call, call])).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["stop_reason"] == "token_budget" and result["tool_calls"] == 1


def test_over_budget_paid_response_is_preserved_in_checkpoint(monkeypatch):
    from app.config import settings
    from app.agent.checkpoint import load_checkpoint, decode_state
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 100)

    class Overflow(MockLLM):
        def chat(self, messages, **kwargs):
            response = super().chat(messages, **kwargs)
            response.tokens_in = 200
            return response

    result = Agent(llm=Overflow(plan=[{"type": "final", "report": {"conclusion": "paid-response"}}])).run(
        1, date(2015, 6, 1), date(2015, 6, 14))
    saved = decode_state(load_checkpoint(result["run_id"]))
    assert saved["tokens_in"] == 200 and "paid-response" in saved["messages"][-1]["content"]
    assert result["report"]["report_status"] == "incomplete"


def test_context_compaction_preserves_evidence_and_latest_correction():
    from app.agent.context import compact_context
    from app.agent.investigation import InvestigationState
    inv = InvestigationState()
    data = {"series": [{"date": "2015-06-01", "uv": 3}], "summary": {"current": {"uv": 3}}}
    inv.observe_tool(1, "metric", {"ok": True, "data": data, "text": "重复摘要" * 200}, None)
    messages = [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]
    messages += [{"role": "user", "content": "旧状态" * 1000} for _ in range(5)]
    messages += [{"role": "assistant", "content": "bad final"}, {"role": "user", "content": "final 未通过：修正引用"},
                 {"role": "user", "content": "[预算收尾]old budget"}]
    compact = compact_context(messages, inv)
    assert compact[-2]["content"] == "bad final" and "修正引用" in compact[-1]["content"]
    assert len(json.dumps(compact)) < len(json.dumps(messages)) / 4
    assert inv.evidence["metric#1"]["data"] == data  # 投影不能篡改存档
    assert '"uv":3' in compact[2]["content"] and '"series":' in compact[2]["content"]
    inv.evidence["metric#1"]["data"]["series"] *= 20
    long_context = compact_context(messages, inv)
    assert '"series":' not in long_context[2]["content"] and "不可据此描述逐日走势" in long_context[2]["content"]
    assert len(inv.evidence["metric#1"]["data"]["series"]) == 20


def test_adaptive_mock_keeps_structured_evidence_with_current_prompt():
    from app.agent.context import CONTEXT_MAX_CHARS

    class InspectingMock(MockLLM):
        def chat(self, messages, **kwargs):
            self.last_messages = copy.deepcopy(messages)
            return super().chat(messages, **kwargs)

    llm = InspectingMock()
    result = Agent(llm=llm).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok", result["quality"]
    assert sum(len(m["content"]) for m in llm.last_messages) <= CONTEXT_MAX_CHARS
    assert any("[结构化证据 metric#1]" in m["content"] for m in llm.last_messages)
    assert result["report"]["facts"][0]["value"] == result["evidence"]["metric#1"]["data"]["summary"]["current"]["cvr"]


def test_adaptive_mock_uses_json_not_display_text_and_does_not_invent_zero():
    from app.agent.context import append_tool_result

    messages = [{"content": '"call_id": "metric#1"; "call_id": "funnel#2"'}]
    missing = MockLLM._adaptive_step(messages)
    assert "facts" not in missing["report"]
    append_tool_result(messages, "metric", {"ok": True, "text": "窗口汇总: {'cvr': 999}"},
                       call_id="metric#1", evidence={"data": {"summary": {"current": {"cvr": 4}}}})
    assert MockLLM._adaptive_step(messages)["report"]["facts"][0]["value"] == 4


def test_agent_can_finalize_from_relevant_peer_branch_only():
    """没有固定 metric/funnel 打卡：只要分支证据充分，peer 路径也可完成。"""
    report = _valid_report("peer#1", "own.uv", 14000)
    plan = [
        {
            "type": "tool_call", "reasoning": "判断是否为类目共同波动",
            "hypothesis": _hypothesis(), "tool": "peer",
            "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"},
        },
        {"type": "final", "report": report},
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok"
    assert result["tools_used"] == ["peer"]


def test_agent_max_steps_guard():
    # Mock 永远返回 tool_call → 必须被 max_steps 截停
    plan = [
        {"type": "tool_call", "tool": "metric",
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}}
        for _ in range(50)
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["stop_reason"] == "max_steps"
    assert result["status"] == "incomplete"
    assert result["steps"] <= 8  # AGENT_MAX_STEPS 默认 8


def test_agent_invalid_json_recovers():
    plan = [
        {"type": "garbage", "foo": 1},  # 非法类型 → 循环给提示
        {"type": "tool_call", "tool": "metric", "hypothesis": _hypothesis(),
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "tool_call", "tool": "funnel", "hypothesis": _hypothesis(refs=["metric#2"]),
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "final", "report": _valid_report("metric#2", "summary.current.uv", 14000)},
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok"
    assert result["stop_reason"] == "final"
    assert result["quality"]["passed"] is True


def test_agent_final_without_evidence_is_rejected():
    # 没有成功证据时，即使连续 final 也不能被放行。
    plan = [
        {"type": "final", "report": {"conclusion": "直接下结论"}},
        {"type": "final", "report": {"conclusion": "直接下结论2"}},
        {"type": "final", "report": {"conclusion": "最终被接受"}},
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["stop_reason"] == "insufficient_evidence"
    assert result["status"] == "incomplete"
    assert result["steps"] == 3


def test_agent_persists_run_and_report():
    plan = [
        {"type": "tool_call", "tool": "metric", "hypothesis": _hypothesis(),
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "final", "report": _valid_report()},
    ]
    from app.db import read_session
    from app.models import AgentRun, DiagnosticReport

    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    with read_session() as s:
        run = s.query(AgentRun).filter_by(run_id=result["run_id"]).one()
        assert run.status == "succeeded"
        assert run.tool_calls == 1
        report = s.query(DiagnosticReport).filter_by(run_id=result["run_id"]).one()
        assert report.item_id == 1


@pytest.mark.parametrize("correct_after_nudge", [True, False])
def test_agent_rejects_overclaim_then_preserves_uncertainty_or_stops(correct_after_nudge):
    from app.db import read_session
    from app.models import AgentCheckpoint, DiagnosticReport

    invalid = _valid_report()
    invalid.update(report_version=2, report_status="quality_checked")
    invalid["conclusion"] = "类目大盘正常，排除大盘影响。"
    valid = _valid_report()
    valid.update(report_version=999, report_status="forged")
    valid["analysis"]["evidence_limits"] = {"forged_proof": "根因已确认"}
    valid["hypotheses"] = []  # 可以只给事实与下一步核查，不强造原因。
    plan = [
        {"type": "tool_call", "tool": "metric",
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "final", "report": invalid},
        {"type": "final", "report": valid if correct_after_nudge else invalid},
    ]
    if not correct_after_nudge:
        plan.append({"type": "final", "report": invalid})
    before = copy.deepcopy(plan)
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert plan == before
    assert "大盘正常" not in result["report"]["conclusion"]
    assert result["report"]["analysis"]["attribution_status"] == "uncertain"
    assert result["report"]["report_version"] == 2
    assert result["report"]["report_status"] == ("quality_checked" if correct_after_nudge else "incomplete")
    with read_session() as s:
        cp = s.query(AgentCheckpoint).filter_by(run_id=result["run_id"]).one()
        saved = s.query(DiagnosticReport).filter_by(run_id=result["run_id"]).one()
        assert json.loads(cp.result_json) == result
        assert json.loads(saved.content_json) == result["report"]
        if correct_after_nudge:
            assert result["status"] == "ok"
            assert result["steps"] == 3
            assert cp.status == "completed"
            analysis = result["report"]["analysis"]
            assert analysis["primary_hypothesis_id"] is None
            assert "forged_proof" not in analysis["evidence_limits"]
            assert "gmv_proxy_not_loss" in analysis["evidence_limits"]
            assert all(note in analysis["limitations"] for note in analysis["evidence_limits"].values())
        else:
            assert result["status"] == "incomplete"
            assert result["stop_reason"] == "insufficient_evidence"
            assert cp.status == "stopped"


def test_agent_resumes_from_last_checkpoint_without_repeating_tool():
    """进程在下一轮 LLM 调用时退出，重启后从已保存的 metric 证据继续。"""
    run_id = "checkpoint-resume-test"
    tool_decision = {
        "type": "tool_call", "tool": "metric", "hypothesis": _hypothesis(),
        "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"},
    }

    class CrashAfterTool(MockLLM):
        def chat(self, messages, *, json_mode=True, timeout=None, max_tokens=None):
            if self.idx >= 1:
                raise SystemExit("simulate process death")
            return super().chat(messages, json_mode=json_mode, timeout=timeout)

    registry = __import__("app.agent", fromlist=["default_registry"]).default_registry()
    original_execute = registry.execute
    calls = {"count": 0}

    def counted_execute(*args, **kwargs):
        calls["count"] += 1
        return original_execute(*args, **kwargs)

    registry.execute = counted_execute
    try:
        Agent(llm=CrashAfterTool(plan=[tool_decision]), registry=registry).run(
            1, date(2015, 6, 1), date(2015, 6, 14), run_id=run_id,
        )
    except SystemExit:
        pass
    else:
        raise AssertionError("expected simulated process death")

    result = Agent(
        llm=MockLLM(plan=[{"type": "final", "report": _valid_report()}]),
        registry=registry,
    ).run(1, date(2015, 6, 1), date(2015, 6, 14), run_id=run_id)

    assert result["status"] == "ok"
    assert result["steps"] == 2
    assert result["tool_calls"] == 1
    assert calls["count"] == 1  # 恢复后没有重复执行已经成功的 metric

    from app.db import read_session
    from app.models import AgentCheckpoint, ToolCallLog

    with read_session() as s:
        checkpoint = s.query(AgentCheckpoint).filter_by(run_id=run_id).one()
        assert checkpoint.status == "completed"
        assert s.query(ToolCallLog).filter_by(run_id=run_id).count() == 1

    class MustNotRun(MockLLM):
        def chat(self, messages, *, json_mode=True, timeout=None, max_tokens=None):
            raise SystemExit("completed checkpoint should bypass the model")

    cached = Agent(llm=MustNotRun(), registry=registry).run(
        1, date(2015, 6, 1), date(2015, 6, 14), run_id=run_id,
    )
    assert cached == result
    assert calls["count"] == 1
