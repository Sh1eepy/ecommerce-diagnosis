"""Agent Loop 测试（MockLLM 全离线）。"""
from datetime import date

from app.agent.agent import Agent
from app.agent.tool import ToolRegistry
from app.llm.mock import MockLLM


def _plan_agent(plan):
    return Agent(llm=MockLLM(plan=plan))


def test_agent_happy_path():
    plan = [
        {"type": "tool_call", "reasoning": "先看历史趋势",
         "tool": "metric", "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "tool_call", "reasoning": "定位漏斗环节",
         "tool": "funnel", "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "tool_call", "reasoning": "维度拆解",
         "tool": "dimension", "args": {"item_id": 1, "dimension": "new_user", "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "final", "report": {
            "facts": [{"point": "cvr 从7%降到4%", "evidence": "metric 工具"}],
            "analysis": {"key_finding": "加购到成交转化下降", "impact": "GMV 下滑"},
            "conclusion": "成交环节转化率下降是主因",
            "suggestions": ["优化支付页", "检查库存"],
        }},
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok"
    assert result["stop_reason"] == "final"
    assert result["tool_calls"] == 3
    assert result["report"]["conclusion"] == "成交环节转化率下降是主因"
    assert result["report"]["suggestions"]


def test_agent_max_steps_guard():
    # Mock 永远返回 tool_call → 必须被 max_steps 截停
    plan = [
        {"type": "tool_call", "tool": "metric",
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}}
        for _ in range(50)
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["stop_reason"] == "max_steps"
    assert result["steps"] <= 8  # AGENT_MAX_STEPS 默认 8


def test_agent_invalid_json_recovers():
    plan = [
        {"type": "garbage", "foo": 1},  # 非法类型 → 循环给提示
        {"type": "tool_call", "tool": "metric",
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "tool_call", "tool": "funnel",
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "final", "report": {"conclusion": "ok after recovery"}},
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["status"] == "ok"
    assert result["stop_reason"] == "final"
    assert result["report"]["conclusion"] == "ok after recovery"


def test_agent_final_without_critical_tools_is_nudged():
    # 直接 final 但没查 metric/funnel → 最多被 workflow 提示 2 次，之后接受
    plan = [
        {"type": "final", "report": {"conclusion": "直接下结论"}},
        {"type": "final", "report": {"conclusion": "直接下结论2"}},
        {"type": "final", "report": {"conclusion": "最终被接受"}},
    ]
    result = _plan_agent(plan).run(1, date(2015, 6, 1), date(2015, 6, 14))
    assert result["stop_reason"] == "final"
    assert result["steps"] == 3  # 前两轮被 nudge，第三轮接受
    assert result["report"]["conclusion"] == "最终被接受"


def test_agent_persists_run_and_report():
    plan = [
        {"type": "tool_call", "tool": "metric",
         "args": {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"}},
        {"type": "final", "report": {"conclusion": "落库验证"}},
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
