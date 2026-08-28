"""指标计算口径测试。"""
import pytest
from datetime import date
from decimal import Decimal
import json

from app.metrics.registry import compute_metrics
from app.metrics.windows import compare_windows, paired_windows


def test_cvr_and_rates():
    row = {"uv": 1000, "view_count": 3000, "addtocart_count": 90, "transaction_count": 80, "gmv": 8000.0}
    out = compute_metrics(row, ["cvr", "addcart_rate", "cart_to_pay", "gmv", "click_rate", "per_user_views", "avg_price"])
    assert out["cvr"] == 8.0
    assert out["addcart_rate"] == 9.0
    assert out["cart_to_pay"] == round(80 * 100.0 / 90, 4)
    assert out["gmv"] == 8000.0
    assert out["click_rate"] == round(1000 * 100.0 / 3000, 4)
    assert out["per_user_views"] == 3.0
    assert out["avg_price"] == 100.0  # 客单价 = GMV / 成交笔数


def test_division_by_zero_returns_zero():
    row = {"uv": 0, "view_count": 0, "addtocart_count": 0, "transaction_count": 0, "gmv": 0.0}
    out = compute_metrics(row, ["cvr", "cart_to_pay", "click_rate", "avg_price"])
    assert out["cvr"] == 0.0
    assert out["cart_to_pay"] == 0.0
    assert out["click_rate"] == 0.0
    assert out["avg_price"] == 0.0


def test_base_columns_pass_through():
    row = {"uv": 7, "view_count": 9, "addtocart_count": 2, "transaction_count": 1}
    out = compute_metrics(row, ["uv", "transaction_count"])
    assert out["uv"] == 7
    assert out["transaction_count"] == 1


def test_mysql_decimal_counts_remain_json_numbers_and_trigger_small_sample_limit():
    from app.agent.investigation import InvestigationState
    from app.agent.quality import evidence_limits
    rows = [{"date": "2024-03-01", "uv": Decimal(15), "transaction_count": Decimal(1), "gmv": Decimal("12.75")},
            {"date": "2024-03-02", "uv": Decimal(24), "transaction_count": Decimal(0), "gmv": Decimal(0)}]
    summary = compare_windows(rows, date(2024, 3, 2), date(2024, 3, 2), ["uv", "gmv", "cvr"])
    decoded = json.loads(json.dumps(summary))  # 无 default=str 才能证明返回契约兼容
    assert type(decoded["sample_counts"]["previous"]["transaction_count"]) is int
    assert decoded["previous"]["gmv"] == 12.75 and decoded["current"]["uv"] == 24
    investigation = InvestigationState()
    investigation.observe_tool(1, "metric", {"ok": True, "data": {"summary": decoded}}, None)
    assert "small_sample" in evidence_limits(investigation.evidence)


def test_unknown_metric_raises():
    with pytest.raises(KeyError):
        compute_metrics({"uv": 1}, ["not_a_metric"])


def test_window_boundaries_include_leap_day_and_reject_underflow():
    assert paired_windows(date(2024, 3, 1), date(2024, 3, 2))["previous"] == (date(2024, 2, 28), date(2024, 2, 29))
    with pytest.raises(ValueError):
        paired_windows(date.min, date.min)


def test_zero_baseline_is_not_infinite_growth_and_missing_denominator_is_unknown():
    rows = [{"date": "2024-03-01", "uv": 10, "transaction_count": 0},
            {"date": "2024-03-02", "uv": 10, "transaction_count": 2}]
    result = compare_windows(rows, date(2024, 3, 2), date(2024, 3, 2), ["cvr", "transaction_count"])
    assert result["changes"]["cvr"] == {"delta": 20, "delta_unit": "percentage_points",
                                         "relative_change_pct": None, "status": "zero_baseline"}
    rows[0]["uv"] = 0
    result = compare_windows(rows, date(2024, 3, 2), date(2024, 3, 2), ["cvr"])
    assert result["changes"]["cvr"]["status"] == "undefined_denominator"
    assert result["changes"]["cvr"]["delta"] is None
