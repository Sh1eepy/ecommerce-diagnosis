"""DimensionTool：按维度拆解商品指标，寻找异常集中点。"""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from app.agent.tools._common import (
    parse_item_window,
    tool_parameters,
    validate_dimension,
    validate_metrics,
)
from app.metrics import compute


name = "dimension"
description = (
    "按维度拆解商品在窗口内的指标，寻找异常集中点。"
    "当前可用维度: day_type(工作日/周末), new_user(新老用户), category(类目)；"
    "预留: channel(渠道), device(设备), user_type(用户), activity(活动)。"
)
parameters = tool_parameters(metrics=True, dimension=True)


def query_dimension(item_id, dimension, start_date, end_date, metrics=None):
    dimension = validate_dimension(dimension)
    item_id, start, end = parse_item_window(item_id, start_date, end_date)
    metric_names = validate_metrics(metrics, ["uv", "addcart_rate", "cvr", "gmv"])

    rows = compute.dimension_breakdown(item_id, start, end, dimension, metric_names)
    lines = [f"商品 {item_id} 按 {dimension} 拆解（{start}~{end}）:"]
    for r in rows:
        parts = [f"  {r['dimension']}"] + [f"{m}={r[m]}" for m in metric_names]
        lines.append(" ".join(parts))
    by_dimension = {str(r["dimension"]): r for r in rows}
    return {
        "ok": True,
        "text": "\n".join(lines),
        "rows": len(rows),
        "data": {"rows": rows, "by_dimension": by_dimension, **by_dimension},
    }


def DimensionTool() -> StructuredTool:
    """返回原生 LangChain 工具；业务入口统一由注册表校验与审计。"""
    return StructuredTool(name=name, description=description, args_schema=parameters, func=query_dimension)
