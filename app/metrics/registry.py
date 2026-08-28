"""指标注册表：口径唯一来源。

派生指标统一在 Python 中计算（registry.compute_metrics），
保证 日序列 / 窗口汇总 / 维度拆解 三处口径一致、可单测。
"""
from __future__ import annotations

from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from typing import Literal, get_args

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

# 数据层与工具共用枚举；导入另允许 all，拆解工具不接受 all。
BreakdownDimension = Literal["day_type", "new_user", "category", "channel", "device", "user_type", "activity"]
DailyStatDimension = Literal["all", BreakdownDimension]
ALLOWED_DIMENSIONS = frozenset(get_args(BreakdownDimension))

# 派生指标公式（r 为原始聚合行，含 BASE_COLUMNS 键）
def _safe_div(a: float, b: float) -> float:
    return round(a / b, 4) if b else 0.0


# 分子、分母、比例因子：计算公式与比较时的分母有效性检查共用。
RATIO_COMPONENTS = {
    "per_user_views": ("view_count", "uv", 1),
    "click_rate": ("uv", "view_count", 100),
    "addcart_rate": ("addtocart_count", "uv", 100),
    "cvr": ("transaction_count", "uv", 100),
    "cart_to_pay": ("transaction_count", "addtocart_count", 100),
    "avg_price": ("gmv", "transaction_count", 1),
}

KNOWN_METRICS = frozenset(BASE_COLUMNS) | frozenset(RATIO_COMPONENTS)


def compute_metrics(row: dict, names: list[str]) -> dict:
    """从原始聚合行计算指定指标（基础列透传）。

    注意：MySQL 的 SUM() 返回 Decimal、SQLite 返回 int/float，
    派生公式统一转 float 规避方言差异。
    """
    out: dict = {}
    frow = None
    for name in names:
        if name not in KNOWN_METRICS:
            raise KeyError(f"未知指标: {name}")
        if name in BASE_COLUMNS:
            value = row.get(name, 0)
            # MySQL SUM(integer) 也是 Decimal；不要把计数交给 JSON default=str
            # 变成字符串，否则小样本判断与标准 JSON 输出会与 SQLite 不一致。
            if isinstance(value, Decimal):
                value = int(value) if name != "gmv" and value == value.to_integral_value() else float(value)
            out[name] = value
        else:
            if frow is None:
                frow = {k: float(v) for k, v in row.items() if k in BASE_COLUMNS}
            numerator, denominator, scale = RATIO_COMPONENTS[name]
            value = frow.get(numerator, 0) if numerator == "gmv" else frow[numerator]
            out[name] = _safe_div(value * scale, frow[denominator])
    return out
