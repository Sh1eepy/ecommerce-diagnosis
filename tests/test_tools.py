"""Tool 测试（基于 seeded SQLite 库）。"""
from datetime import date

import pytest

from app.agent import default_registry
from app.tracing import set_run_id


def _reg():
    return default_registry()


@pytest.mark.parametrize("name,extra", [
    ("metric", {}), ("funnel", {}), ("peer", {}), ("dimension", {"dimension": "new_user"}),
])
def test_langchain_tool_preserves_domain_schema_and_result(name, extra):
    from langchain_core.tools import BaseTool
    from app.agent.langchain_tools import invoke_tool

    registry = _reg()
    args = {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14", **extra}
    native = registry.get(name)
    assert isinstance(native, BaseTool)
    assert native.args_schema == registry.input_schema(name)
    direct = registry.execute(name, args, run_id="domain-tool", step=1)
    result = invoke_tool(registry, name, args, run_id="framework-tool", step=1)
    assert {k: v for k, v in result.items() if k != "_meta"} == {
        k: v for k, v in direct.items() if k != "_meta"}


@pytest.mark.parametrize("change", [
    {"item_id": True}, {"start_date": "2015/06/01"}, {"start_date": "2015-06-20"},
    {"run_id": "forged"}, {"step": 99}, {"metrics": ["unknown"]},
])
def test_langchain_tool_keeps_invalid_input_error_envelope(change):
    from app.agent.langchain_tools import invoke_tool

    registry = _reg()
    args = {"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14", **change}
    result = invoke_tool(registry, "metric", args, run_id="invalid-framework-tool", step=1)
    direct = registry.execute("metric", args, run_id="invalid-domain-tool", step=1)
    assert result["ok"] is False
    assert result["data"] is None and result["text"] == direct["text"]


def test_metric_tool_returns_series():
    res = _reg().execute("metric", {
        "item_id": 1,
        "start_date": "2015-06-01",
        "end_date": "2015-06-14",
    }, run_id=set_run_id("t1"), step=1)
    assert res["ok"] is True
    assert res["rows"] == 14
    assert "cvr" in res["text"]
    data = res["data"]
    assert data["series"][0]["uv"] == 1000
    assert data["series"][-1]["cvr"] == 4.0  # item1 最后一天 cvr 4%
    assert data["coverage"]["expected_days"] == 14
    assert data["coverage"]["observed_days"] == 14
    assert data["coverage"]["dates_without_rows"] == []
    assert "近似指标" in res["text"]


def test_funnel_tool():
    res = _reg().execute("funnel", {
        "item_id": 1,
        "start_date": "2015-06-01",
        "end_date": "2015-06-14",
    }, run_id=set_run_id("t2"), step=1)
    assert res["ok"] is True
    assert res["rows"] == 3
    stages = {s["stage"]: s for s in res["data"]["stages"]}
    assert set(stages) == {"view", "addtocart", "transaction"}


def test_dimension_tool():
    res = _reg().execute("dimension", {
        "item_id": 1,
        "dimension": "new_user",
        "start_date": "2015-06-01",
        "end_date": "2015-06-14",
        "metrics": ["uv", "cvr"],
    }, run_id=set_run_id("t3"), step=1)
    assert res["ok"] is True
    dims = {r["dimension"] for r in res["data"]["rows"]}
    assert dims == {"new", "returning"}
    assert res["data"]["new"]["uv"] > 0


def test_invalid_item_id_rejected():
    res = _reg().execute("metric", {
        "item_id": -5,
        "start_date": "2015-06-01",
        "end_date": "2015-06-14",
    }, run_id=set_run_id("t4"), step=1)
    assert res["ok"] is False
    assert "item_id" in res["text"]


def test_invalid_date_rejected():
    res = _reg().execute("metric", {
        "item_id": 1,
        "start_date": "2015/06/01",
        "end_date": "2015-06-14",
    }, run_id=set_run_id("t5"), step=1)
    assert res["ok"] is False


def test_unknown_metric_rejected():
    res = _reg().execute("metric", {
        "item_id": 1,
        "start_date": "2015-06-01",
        "end_date": "2015-06-14",
        "metrics": ["hacked_metric"],
    }, run_id=set_run_id("t6"), step=1)
    assert res["ok"] is False


def test_unknown_tool_rejected():
    res = _reg().execute("shell", {"cmd": "rm -rf /"}, run_id=set_run_id("t7"), step=1)
    assert res["ok"] is False
    assert "白名单" in res["text"]


def test_date_range_too_wide_rejected():
    res = _reg().execute("metric", {
        "item_id": 1,
        "start_date": "2015-01-01",
        "end_date": "2015-12-31",
    }, run_id=set_run_id("t8"), step=1)
    assert res["ok"] is False


def test_peer_tool():
    res = _reg().execute("peer", {
        "item_id": 1,
        "start_date": "2015-06-01",
        "end_date": "2015-06-14",
        "metrics": ["uv", "cvr", "gmv"],
    }, run_id=set_run_id("t9"), step=1)
    assert res["ok"] is True
    data = res["data"]
    assert data["category_id"] == 100
    assert data["own"]["uv"] == 14000          # item1: 14天 × 1000
    assert data["category_total"]["uv"] == 28000  # 类目=item1+item2
    assert data["peers"]["uv"] == 14000        # 同行=类目-自身=item2
    assert data["peers"]["cvr"] == 8.0         # item2 平稳，cvr 8%
    assert "history" not in data
    assert "没有同行历史基线" in res["text"]


def test_peer_tool_no_category():
    res = _reg().execute("peer", {
        "item_id": 9999,
        "start_date": "2015-06-01",
        "end_date": "2015-06-14",
    }, run_id=set_run_id("t10"), step=1)
    assert res["ok"] is True
    assert res["data"]["category_id"] is None
    assert "无法" in res["text"]


def test_metric_reports_days_without_rows_without_filling_zeros(monkeypatch):
    from app.metrics import compute

    monkeypatch.setattr(compute, "item_unavailable_periods", lambda *args: [{"date": "2015-06-14"}])
    res = _reg().get("metric").invoke({"item_id": 1, "start_date": "2015-06-13", "end_date": "2015-06-15"})
    assert res["data"]["coverage"] == {
        "expected_days": 3, "observed_days": 2,
        "dates_without_rows": ["2015-06-15"], "missing_days_are_zero": False,
    }
    assert len(res["data"]["series"]) == 2
    assert res["data"]["summary"]["coverage"]["previous"]["observed_days"] == 3
    assert res["data"]["summary"]["changes"]["uv"]["delta"] is None
    assert res["data"]["unavailable_dates"] == ["2015-06-14"]
    assert "观察点" in res["text"]
    assert "直接成因" not in res["text"]


def test_metric_with_no_daily_rows_does_not_assert_complete_zero_activity():
    res = _reg().get("metric").invoke({"item_id": 999999, "start_date": "2015-06-01", "end_date": "2015-06-02"})
    assert res["data"]["coverage"]["observed_days"] == 0
    assert res["data"]["coverage"]["dates_without_rows"] == ["2015-06-01", "2015-06-02"]
    assert res["data"]["coverage"]["missing_days_are_zero"] is False


def test_metric_both_windows_and_percentage_point_change():
    result = _reg().get("metric").invoke({"item_id": 1, "start_date": "2015-06-08", "end_date": "2015-06-14", "metrics": ["cvr", "uv"]})
    summary = result["data"]["summary"]
    assert summary["windows"] == {"current": ["2015-06-08", "2015-06-14"],
                                   "previous": ["2015-06-01", "2015-06-07"]}
    assert summary["coverage"]["previous"]["observed_days"] == 7
    assert summary["changes"]["cvr"]["delta"] == -1.4286
    assert summary["changes"]["cvr"]["delta_unit"] == "percentage_points"
    assert summary["changes"]["uv"]["relative_change_pct"] == 0


def test_metric_current_complete_previous_missing_suppresses_change():
    result = _reg().get("metric").invoke({"item_id": 1, "start_date": "2015-06-01", "end_date": "2015-06-14"})
    summary = result["data"]["summary"]
    assert result["data"]["coverage"]["dates_without_rows"] == []
    assert summary["coverage"]["previous"]["observed_days"] == 0
    assert summary["changes"]["uv"]["delta"] is None
    assert "不能直接解释环比" in result["text"]


def test_tool_queries_reuse_aggregates(monkeypatch):
    from app.metrics import compute
    original, calls = compute._rows, []

    def record(sql, params):
        calls.append(sql)
        return original(sql, params)

    monkeypatch.setattr(compute, "_rows", record)
    for name, queries in (("metric", 3), ("peer", 4)):
        calls.clear()
        assert _reg().get(name).invoke({"item_id": 1, "start_date": "2015-06-08", "end_date": "2015-06-14"})["ok"]
        assert len(calls) == queries  # metric: 双窗口/价格/状态；peer: 类目/自身/类目汇总/TOP。
