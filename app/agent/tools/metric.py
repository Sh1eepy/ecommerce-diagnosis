"""MetricTool：查询商品核心指标与历史趋势。"""
from __future__ import annotations

from app.agent.tool import Tool
from app.agent.tools._common import validate_date, validate_item_id, validate_metrics
from app.metrics import compute
from app.metrics.registry import KNOWN_METRICS

DEFAULT_METRICS = ["uv", "click_rate", "addcart_rate", "cvr", "gmv"]


class MetricTool(Tool):
    name = "metric"
    description = (
        "查询商品在日期窗口内的核心经营指标（UV/点击率/加购率/支付转化率/GMV/客单价）"
        "的每日序列，以及窗口汇总和上一等长窗口对比。用于确认异常走势与历史趋势。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "item_id": {"type": "integer", "description": "商品 ID"},
            "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
            "metrics": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选指标: " + ", ".join(sorted(KNOWN_METRICS)),
            },
        },
        "required": ["item_id", "start_date", "end_date"],
    }

    def run(self, item_id, start_date, end_date, metrics=None):
        item_id = validate_item_id(item_id)
        start = validate_date(start_date)
        end = validate_date(end_date)
        metric_names = validate_metrics(metrics, DEFAULT_METRICS)

        series = compute.daily_series(item_id, start, end, metric_names)
        summary = compute.item_summary(item_id, start, end, metric_names)

        lines = [f"商品 {item_id} 日指标序列（{start}~{end}），共 {len(series)} 天:"]
        for row in series:
            parts = [f"  {row['date']}"] + [f"{m}={row[m]}" for m in metric_names]
            lines.append(" ".join(parts))
        lines.append(f"窗口汇总: {summary['current']}")
        if summary.get("previous"):
            lines.append(f"上一等长窗口({summary['window'][0]}~): {summary['previous']}")
        return {
            "ok": True,
            "text": "\n".join(lines),
            "rows": len(series),
            "data": {"series": series, "summary": summary},
        }
