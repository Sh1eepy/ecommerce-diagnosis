"""确定性规则引擎：异常检测不依赖 LLM（可单测、可复现、可解释）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass
class RuleResult:
    metric: str
    rule_id: str
    rule_name: str
    baseline_value: float
    current_value: float
    change_pct: float  # 0~1，越大说明降幅越大
    date_start: date
    date_end: date
    description: str


class ConsecutiveDeclineRule:
    """指标连续 days 天下降，且累计降幅 >= drop_pct。

    例：支付转化率连续3天下降且累计降幅 > 30% → 判定异常。
    """

    def __init__(self, metric: str, days: int = 3, drop_pct: float = 0.30, min_baseline: float = 1.0):
        self.metric = metric
        self.days = days
        self.drop_pct = drop_pct
        self.min_baseline = min_baseline

    @property
    def rule_id(self) -> str:
        return f"consecutive_decline_{self.metric}_{self.days}d_{int(self.drop_pct * 100)}pct"

    def evaluate(self, series: list[tuple[date, float]]) -> RuleResult | None:
        pts = [(d, v) for d, v in series if v is not None]
        if len(pts) < self.days + 1:
            return None
        last = pts[-(self.days + 1):]
        base_date, base_val = last[0]
        declines = all(last[i][1] < last[i - 1][1] for i in range(1, len(last)))
        drop = (base_val - last[-1][1]) / base_val if base_val else 0.0
        if declines and base_val >= self.min_baseline and drop >= self.drop_pct:
            return RuleResult(
                metric=self.metric,
                rule_id=self.rule_id,
                rule_name=f"连续{self.days}天下降>={int(self.drop_pct * 100)}%",
                baseline_value=round(base_val, 4),
                current_value=round(last[-1][1], 4),
                change_pct=round(drop, 4),
                date_start=base_date,
                date_end=last[-1][0],
                description=(
                    f"{self.metric} 由 {base_val:.2f} 连续{self.days}天降至 "
                    f"{last[-1][1]:.2f}，累计降幅 {drop * 100:.1f}%"
                ),
            )
        return None


class PeriodDropRule:
    """近 n 天均值较上一等长窗口下降 >= drop_pct（周环比类）。"""

    def __init__(self, metric: str, days: int = 7, drop_pct: float = 0.30):
        self.metric = metric
        self.days = days
        self.drop_pct = drop_pct

    @property
    def rule_id(self) -> str:
        return f"period_drop_{self.metric}_{self.days}d_{int(self.drop_pct * 100)}pct"

    def evaluate(self, series: list[tuple[date, float]]) -> RuleResult | None:
        pts = [v for _, v in series if v is not None]
        if len(pts) < self.days * 2:
            return None
        cur = sum(pts[-self.days:]) / self.days
        prev = sum(pts[-2 * self.days:-self.days]) / self.days
        drop = (prev - cur) / prev if prev else 0.0
        if drop >= self.drop_pct:
            return RuleResult(
                metric=self.metric,
                rule_id=self.rule_id,
                rule_name=f"近{self.days}日均值较上期下降>={int(self.drop_pct * 100)}%",
                baseline_value=round(prev, 4),
                current_value=round(cur, 4),
                change_pct=round(drop, 4),
                date_start=series[-self.days][0],
                date_end=series[-1][0],
                description=(
                    f"{self.metric} 近{self.days}日均值 {cur:.2f}，"
                    f"较上一同长窗口 {prev:.2f} 下降 {drop * 100:.1f}%"
                ),
            )
        return None


# 默认规则集（可在配置/调用处覆盖）
DEFAULT_RULES = [
    ConsecutiveDeclineRule("cvr", days=3, drop_pct=0.30),
    ConsecutiveDeclineRule("addcart_rate", days=3, drop_pct=0.30),
    ConsecutiveDeclineRule("uv", days=3, drop_pct=0.30),
    PeriodDropRule("cvr", days=7, drop_pct=0.30),
    PeriodDropRule("gmv", days=7, drop_pct=0.30),
]
