"""Agent Loop 测试（MockLLM 全离线）。"""
from datetime import date

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
        "hypotheses": [_hypothesis(refs=[call_id], status="supported", confidence=0.8)],
        "analysis": {
            "primary_hypothesis_id": "H1", "key_finding": "成交环节效率下降",
            "impact": "影响成交", "limitations": ["只能确认相关关系"],
        },
        "conclusion": "成交环节效率下降是当前证据支持的主假设",
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
