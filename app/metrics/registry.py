"""指标注册表：口径唯一来源。

派生指标统一在 Python 中计算（registry.compute_metrics），
保证 日序列 / 窗口汇总 / 维度拆解 三处口径一致、可单测。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml


@lru_cache(maxsize=1)
def load_definitions() -> dict:
    p = Path(__file__).parent / "definitions.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def funnel_stages() -> list[dict]:
    return load_definitions()["funnel_stages"]


def all_metrics() -> dict:
    return dict(load_definitions()["metrics"])


def metric_label(name: str) -> str:
    return all_metrics()[name]["label"]


# 日表基础聚合列（原始量，gmv 已在导入时按"成交笔数×最新价格"落库）
BASE_COLUMNS = ["uv", "view_count", "addtocart_count", "transaction_count", "gmv"]

# 派生指标公式（r 为原始聚合行，含 BASE_COLUMNS 键）
def _safe_div(a: float, b: float) -> float:
    return round(a / b, 4) if b else 0.0


_DERIVED = {
    "per_user_views": lambda r: _safe_div(r["view_count"], r["uv"]),
    "click_rate": lambda r: _safe_div(r["uv"] * 100.0, r["view_count"]),
    "addcart_rate": lambda r: _safe_div(r["addtocart_count"] * 100.0, r["uv"]),
    "cvr": lambda r: _safe_div(r["transaction_count"] * 100.0, r["uv"]),
    "cart_to_pay": lambda r: _safe_div(r["transaction_count"] * 100.0, r["addtocart_count"]),
    "avg_price": lambda r: _safe_div(r.get("gmv", 0), r["transaction_count"]),
}

KNOWN_METRICS = frozenset(BASE_COLUMNS) | frozenset(_DERIVED)


def compute_metrics(row: dict, names: list[str]) -> dict:
    """从原始聚合行计算指定指标（基础列透传）。

    注意：MySQL 的 SUM() 返回 Decimal、SQLite 返回 int/float，
    派生公式统一转 float 规避方言差异。
    """
    out: dict = {}
    for name in names:
        if name not in KNOWN_METRICS:
            raise KeyError(f"未知指标: {name}")
        if name in BASE_COLUMNS:
            out[name] = row.get(name, 0)
        else:
            frow = {k: float(v) for k, v in row.items() if k in BASE_COLUMNS}
            out[name] = _DERIVED[name](frow)
    return out
