"""DimensionTool：按维度拆解商品指标，寻找异常集中点。"""
from __future__ import annotations

from app.agent.tool import Tool
from app.agent.tools._common import (
    validate_date,
    validate_dimension,
    validate_item_id,
    validate_metrics,
)
from app.metrics import compute
from app.metrics.registry import KNOWN_METRICS


class DimensionTool(Tool):
    name = "dimension"
    description = (
        "按维度拆解商品在窗口内的指标，寻找异常集中点。"
        "当前可用维度: day_type(工作日/周末), new_user(新老用户), category(类目)；"
        "预留: channel(渠道), device(设备), user_type(用户), activity(活动)。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "item_id": {"type": "integer", "description": "商品 ID"},
            "dimension": {"type": "string", "description": "维度类型: day_type / new_user / channel / device / user_type / activity"},
            "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            "metrics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选指标: " + ", ".join(sorted(KNOWN_METRICS)),
            },
        },
        "required": ["item_id", "dimension", "start_date", "end_date"],
    }

    def run(self, item_id, dimension, start_date, end_date, metrics=None):
        item_id = validate_item_id(item_id)
        dimension = validate_dimension(dimension)
        start = validate_date(start_date)
        end = validate_date(end_date)
        metric_names = validate_metrics(metrics, ["uv", "addcart_rate", "cvr", "gmv"])

        rows = compute.dimension_breakdown(item_id, start, end, dimension, metric_names)
        lines = [f"商品 {item_id} 按 {dimension} 拆解（{start}~{end}）:"]
        for r in rows:
            parts = [f"  {r['dimension']}"] + [f"{m}={r[m]}" for m in metric_names]
            lines.append(" ".join(parts))
        return {"ok": True, "text": "\n".join(lines), "rows": len(rows), "data": rows}
