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


def _metric_call():
    return {"type": "tool_call", "tool": "metric", "args": {
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}}


def test_repeated_candidate_stops_before_another_paid_correction():
    from app.agent.checkpoint import decode_result, load_checkpoint
    bad = _valid_report()
    bad["conclusion"] = "支付环节转化率为0"
    reordered = dict(reversed(list(copy.deepcopy(bad).items())))
    reordered["conclusion"] = " 支付环节 转化率为0\n"

    class Recorder(MockLLM):
        def chat(self, messages, **kwargs):
            self.last_messages = copy.deepcopy(messages)
            return super().chat(messages, **kwargs)

    llm = Recorder(plan=[_metric_call(), {"type": "final", "report": bad},
                         {"type": "final", "report": reordered},
                         {"type": "final", "report": _valid_report()}])
    result = Agent(llm=llm).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert llm.idx == result["llm_attempts"] == result["steps"] == 3
    assert result["status"] == "incomplete" and result["stop_reason"] == "insufficient_evidence"
    assert "重复修正未取得进展" in result["report"]["analysis"]["key_finding"]
    assert result["report"]["facts"] == []
    feedback = next(m["content"] for m in llm.last_messages if m["content"].startswith("final 未通过"))
    assert "conclusion" in feedback and "支付环节转化率为0" in feedback and "没有支付日志" in feedback
    cp = load_checkpoint(result["run_id"])
    assert cp.status == "stopped" and decode_result(cp) == result


@pytest.mark.parametrize("legacy_correction", [False, True])
def test_repeat_guard_survives_checkpoint_resume_without_new_schema(legacy_correction):
    from app.agent.checkpoint import decode_state, load_checkpoint
    from app.db import write_session
    from app.models import AgentCheckpoint
    bad = _valid_report()
    bad["conclusion"] = "商品下架导致无成交"

    class Crash(MockLLM):
        def chat(self, messages, **kwargs):
            if self.idx == 2:
                raise SystemExit("crash after saved rejection")
            return super().chat(messages, **kwargs)

    rid = f'repeat-guard-{legacy_correction}'
    with pytest.raises(SystemExit):
        Agent(llm=Crash(plan=[_metric_call(), {"type": "final", "report": bad}])).run(
            1, date(2015, 6, 1), date(2015, 6, 14), run_id=rid)
    saved = decode_state(load_checkpoint(rid))
    assert saved["nudges"] == 1 and saved["llm_calls"] == 2
    if legacy_correction:
        with write_session() as s:
            cp = s.query(AgentCheckpoint).filter_by(run_id=rid).one()
            payload = json.loads(cp.state_json)
            payload["messages"][-1]["content"] = "final 未通过证据质量门槛，请修正后重试。问题：证据越界"
            cp.state_json = json.dumps(payload, ensure_ascii=False)
            s.commit()
    llm = MockLLM(plan=[{"type": "final", "report": bad}, {"type": "final", "report": _valid_report()}])
    result = Agent(llm=llm).run(1, date(2015, 6, 1), date(2015, 6, 14), run_id=rid)
    assert llm.idx == 1 and result["steps"] == result["llm_attempts"] == 3
    assert result["tool_calls"] == 1 and result["status"] == "incomplete"
    assert decode_state(load_checkpoint(rid))["tokens_in"] > saved["tokens_in"]


def test_new_evidence_allows_reassessment_but_keeps_correction_budget():
    bad = _valid_report(value=999)
    llm = MockLLM(plan=[_metric_call(), {"type": "final", "report": bad}, _metric_call(),
                       {"type": "final", "report": bad}, {"type": "final", "report": _valid_report()}])
    result = Agent(llm=llm).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok" and llm.idx == 5 and result["tool_calls"] == 2


MIGRATION_SCENARIOS = ("adaptive", "corrected", "rejected", "invalid", "tool_error", "max_steps", "budget")


def _migration_contract(agent_class, scenario, monkeypatch):
    """固定时钟，记录输入/输出契约；基准由迁移前 Agent 在同一隔离 fixture 生成。"""
    import hashlib
    import sys
    from types import SimpleNamespace
    from app.agent.checkpoint import decode_state, load_checkpoint
    from app.config import settings

    monkeypatch.setattr(sys.modules[agent_class.__module__], "time", SimpleNamespace(
        time=lambda: 1_800_000_000.0, perf_counter=lambda: 0.0))
    monkeypatch.setattr(settings, "AGENT_MAX_STEPS", 8)
    monkeypatch.setattr(settings, "AGENT_TOKEN_BUDGET", 20 if scenario == "budget" else 30000)
    monkeypatch.setattr(settings, "AGENT_TOTAL_TIMEOUT_SECONDS", 300.0)
    monkeypatch.setattr(settings, "AGENT_STEP_TIMEOUT_SECONDS", 90.0)
    monkeypatch.setattr(settings, "AGENT_MAX_OUTPUT_TOKENS", 2048)
    call = {"type": "tool_call", "tool": "metric", "args": {
        "item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}}
    valid = {"type": "final", "report": _valid_report()}
    bad = copy.deepcopy(valid)
    bad["report"]["conclusion"] = "类目大盘正常，排除大盘影响。"
    plans = {
        "adaptive": None, "corrected": [call, bad, valid], "rejected": [call, bad, bad, bad],
        "invalid": [{"type": "invalid"}, call,
                    {"type": "final", "report": _valid_report(call_id="metric#2")}],
        "tool_error": [{"type": "tool_call", "tool": "unknown", "args": {}}, call,
                       {"type": "final", "report": _valid_report(call_id="metric#2")}],
        "max_steps": [call] * 8, "budget": [call, valid],
    }
    requests = []

    class Recorder(MockLLM):
        def chat(self, messages, *, json_mode=True, timeout=None, max_tokens=None):
            requests.append(copy.deepcopy({"messages": messages, "json_mode": json_mode,
                                           "timeout": timeout, "max_tokens": max_tokens}))
            return super().chat(messages, json_mode=json_mode, timeout=timeout, max_tokens=max_tokens)

    result = agent_class(llm=Recorder(plan=plans[scenario])).run(1, date(2015, 6, 1), date(2015, 6, 14))
    checkpoint = load_checkpoint(result["run_id"])

    def semantic(value):
        if isinstance(value, dict):
            return {k: semantic(v) for k, v in value.items()
                    if k not in {"run_id", "latency_ms", "llm_duration_ms"}}
        if isinstance(value, list):
            return [semantic(v) for v in value]
        return value

    def digest(value):
        raw = json.dumps(semantic(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode()).hexdigest()

    return {"status": result["status"], "stop_reason": result["stop_reason"],
            "steps": result["steps"], "tool_calls": result["tool_calls"],
            "llm_requests": len(requests), "requests_sha256": digest(requests),
            "result_sha256": digest(result), "checkpoint_status": checkpoint.status,
            "checkpoint_sha256": digest(decode_state(checkpoint))}


@pytest.mark.parametrize("scenario", MIGRATION_SCENARIOS)
def test_langgraph_matches_pre_migration_contract(scenario, monkeypatch):
    from pathlib import Path

    baseline = json.loads((Path(__file__).parent / "fixtures" / "agent_loop_contract.json").read_text(encoding="utf-8"))
    actual, original = _migration_contract(Agent, scenario, monkeypatch), baseline[scenario]
    if scenario in {"corrected", "rejected"}:
        # 后续业务修复有意改变修正提示和重复候选的停止时机；历史基准不可重录。
        changed = {key for key in actual if actual[key] != original[key]}
        expected_changes = {"requests_sha256", "checkpoint_sha256"}
        if scenario == "rejected":
            expected_changes |= {"steps", "llm_requests", "result_sha256"}
            assert actual["steps"] == original["steps"] - 1 == 3
            assert actual["llm_requests"] == original["llm_requests"] - 1 == 3
        assert changed == expected_changes
        # 新行为的原句提示、结果与恢复约束由下方针对性测试覆盖。
    else:
        assert actual == original


def test_langgraph_executes_nodes_and_langchain_interfaces(monkeypatch):
    from app.agent.graph import InvestigationGraph
    from app.llm.langchain_adapter import ProviderChatModel
    from langchain_core.tools import StructuredTool

    visited, models, tools = [], [], []
    for name in ("prepare", "model", "tools", "review", "checkpoint"):
        original = getattr(InvestigationGraph, name)

        def traced(self, state, runtime, _name=name, _original=original):
            visited.append(_name)
            return _original(self, state, runtime)

        monkeypatch.setattr(InvestigationGraph, name, traced)
    original_generate = ProviderChatModel._generate
    original_invoke = StructuredTool.invoke

    def generate(self, *args, **kwargs):
        models.append(self._llm_type)
        return original_generate(self, *args, **kwargs)

    def invoke(self, *args, **kwargs):
        tools.append(self.name)
        return original_invoke(self, *args, **kwargs)

    monkeypatch.setattr(ProviderChatModel, "_generate", generate)
    monkeypatch.setattr(StructuredTool, "invoke", invoke)
    result = Agent(llm=MockLLM()).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok"
    assert visited == ["prepare", "model", "tools", "checkpoint"] * 2 + ["prepare", "model", "review"]
    assert models == ["ecommerce-provider"] * 3
    assert tools == ["metric", "funnel"]


def test_framework_calls_disable_ambient_remote_tracing(monkeypatch):
    from langsmith.run_helpers import get_tracing_context
    from langsmith import tracing_context
    from app.agent import default_registry
    from app.llm.langchain_adapter import invoke_chat

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    # 自动创建远程 tracer 就失败，不依赖网络失败来掩盖意外外发。
    def forbidden_tracer(*args, **kwargs):
        pytest.fail("不应创建远程 LangSmith tracer")

    monkeypatch.setattr("langchain_core.tracers.langchain.LangChainTracer.__init__", forbidden_tracer)
    observations = []

    class LocalOnly(MockLLM):
        def chat(self, messages, **kwargs):
            observations.append(get_tracing_context()["enabled"])
            return super().chat(messages, **kwargs)

    registry = default_registry()
    execute = registry.execute

    def traced_tool(*args, **kwargs):
        observations.append(get_tracing_context()["enabled"])
        return execute(*args, **kwargs)

    monkeypatch.setattr(registry, "execute", traced_tool)
    with tracing_context(enabled=True):
        result = Agent(llm=LocalOnly(), registry=registry).run(1, date(2015, 6, 1), date(2015, 6, 14))
        # 反馈路径独立调用适配器，同样需要抑制环境 tracing。
        invoke_chat(LocalOnly(plan=[{"type": "final"}]), [{"role": "user", "content": "反馈"}])
        assert get_tracing_context()["enabled"] is True
    assert result["status"] == "ok"
    assert observations == [False] * 6
