"""Tool 参数校验：强类型 + 白名单，防注入与资源滥用。"""
from __future__ import annotations

from datetime import date

from app.metrics.registry import ALLOWED_DIMENSIONS, KNOWN_METRICS
from app.metrics.windows import DATE_RANGE_MAX_DAYS, check_range


def tool_parameters(*, metrics: bool = False, dimension: bool = False) -> dict:
    """Agent 和 MCP 共用的输入契约；跨字段日期关系仍由业务层校验。"""
    props = {
        "item_id": {"type": "integer", "minimum": 1, "description": "商品 ID"},
        "start_date": {"type": "string", "format": "date", "minLength": 10, "maxLength": 10,
                       "description": "开始日期 YYYY-MM-DD"},
        "end_date": {"type": "string", "format": "date", "minLength": 10, "maxLength": 10,
                     "description": f"结束日期 YYYY-MM-DD；与开始日期相差不超过 {DATE_RANGE_MAX_DAYS} 天"},
    }
    required = ["item_id", "start_date", "end_date"]
    if metrics:
        props["metrics"] = {
            "type": "array", "items": {"type": "string", "enum": sorted(KNOWN_METRICS)},
            "minItems": 1, "maxItems": len(KNOWN_METRICS), "uniqueItems": True,
            "description": "可选指标，省略时使用工具默认指标；GMV 为近似口径",
        }
    if dimension:
        props["dimension"] = {"type": "string", "enum": sorted(ALLOWED_DIMENSIONS),
                              "description": "当前有数据的维度: day_type / new_user / category；其他维度预留"}
        required.append("dimension")
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


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


def parse_item_window(item_id, start_date, end_date) -> tuple[int, date, date]:
    """四个读取工具共用的执行前校验，不改变各工具自己的业务查询。"""
    item_id = validate_item_id(item_id)
    start, end = validate_date(start_date), validate_date(end_date)
    check_range(start, end)
    return item_id, start, end


def comparison_notes(summary: dict, label: str) -> list[str]:
    """以一致术语展示窗口覆盖，不把无记录当零，也不把覆盖齐全当审计通过。"""
    lines = []
    for key, title in (("current", "当前"), ("previous", "上一")):
        info = summary["coverage"][key]
        lines.append(f"{label}{title}窗口记录覆盖: {info['observed_days']}/{info['expected_days']} 天")
    if summary["comparison_status"] != "observed_days_covered":
        lines.append(f"{label}前后窗口记录不足，变化量不可用；可能无事件或漏数据，不能直接解释环比。")
    else:
        lines.append(f"{label}记录层面的变化: {summary['changes']}；比例差为百分点，记录齐全不等于数据已完整审计。")
    return lines


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
    allowed = ALLOWED_DIMENSIONS
    d = str(v).strip()
    if d not in allowed:
        raise ValueError(f"不支持的维度类型: {d}（可选: {', '.join(sorted(allowed))}）")
    return d
