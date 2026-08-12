"""Tool 参数校验：强类型 + 白名单，防注入与资源滥用。"""
from __future__ import annotations

from datetime import date

from app.metrics.registry import KNOWN_METRICS


def validate_item_id(v) -> int:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"item_id 必须为整数: {v!r}")
    n = int(v)
    if n <= 0:
        raise ValueError(f"item_id 必须为正整数: {v!r}")
    return n


def validate_date(v) -> date:
    if isinstance(v, date):
        return v
    s = str(v).strip()
    try:
        return date.fromisoformat(s)
    except ValueError as e:
        raise ValueError(f"日期格式必须为 YYYY-MM-DD: {s!r}") from e


def validate_metrics(v, default: list[str]) -> list[str]:
    if v is None:
        return list(default)
    if not isinstance(v, list) or not v:
        raise ValueError("metrics 必须是非空数组")
    out = []
    for m in v:
        m = str(m)
        if m not in KNOWN_METRICS:
            raise ValueError(f"未知指标: {m}（可选: {', '.join(sorted(KNOWN_METRICS))}）")
        out.append(m)
    return out


def validate_dimension(v) -> str:
    allowed = {"day_type", "new_user", "channel", "device", "user_type", "activity"}
    d = str(v).strip()
    if d not in allowed:
        raise ValueError(f"不支持的维度类型: {d}（可选: {', '.join(sorted(allowed))}）")
    return d
