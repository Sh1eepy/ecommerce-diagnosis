"""Tool 测试（基于 seeded SQLite 库）。"""
from datetime import date

from app.agent import default_registry
from app.tracing import set_run_id


def _reg():
    return default_registry()


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
    dims = {r["dimension"] for r in res["data"]}
    assert dims == {"new", "returning"}


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


def test_peer_tool_no_category():
    res = _reg().execute("peer", {
        "item_id": 9999,
        "start_date": "2015-06-01",
        "end_date": "2015-06-14",
    }, run_id=set_run_id("t10"), step=1)
    assert res["ok"] is True
    assert res["data"]["category_id"] is None
    assert "无法" in res["text"]
