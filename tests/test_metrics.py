"""指标计算口径测试。"""
import pytest

from app.metrics.registry import compute_metrics


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


def test_unknown_metric_raises():
    with pytest.raises(KeyError):
        compute_metrics({"uv": 1}, ["not_a_metric"])
