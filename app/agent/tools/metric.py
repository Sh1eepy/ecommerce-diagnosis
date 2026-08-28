"""MetricTool：查询商品核心指标与历史趋势。"""
from __future__ import annotations

from app.agent.tool import Tool
from app.agent.tools._common import comparison_notes, tool_parameters, parse_item_window, validate_metrics
from app.metrics import compute

DEFAULT_METRICS = ["uv", "click_rate", "addcart_rate", "cvr", "gmv"]


class MetricTool(Tool):
    name = "metric"
    description = (
        "查询商品在日期窗口内的核心经营指标（UV/点击率/加购率/支付转化率/GMV/客单价）"
        "的每日序列，以及窗口汇总和上一等长窗口对比。用于确认异常走势与历史趋势。"
    )
    parameters = tool_parameters(metrics=True)

    def run(self, item_id, start_date, end_date, metrics=None):
        item_id, start, end = parse_item_window(item_id, start_date, end_date)
        metric_names = validate_metrics(metrics, DEFAULT_METRICS)

        comparison = compute.item_comparison(item_id, start, end, metric_names)
        series, summary = comparison["series"], comparison["summary"]
        coverage = summary["coverage"]["current"]  # 保留旧 evidence_ref 路径。

        lines = [f"商品 {item_id} 日指标序列（{start}~{end}），共 {len(series)} 天:"]
        for row in series:
            parts = [f"  {row['date']}"] + [f"{m}={row[m]}" for m in metric_names]
            lines.append(" ".join(parts))
        lines.append(f"窗口汇总: {summary['current']}")
        if summary.get("previous"):
            lines.append(f"上一等长窗口({summary['window'][0]}~): {summary['previous']}")
        lines.extend(comparison_notes(summary, "商品"))
        lines.append("GMV 为成交笔数乘商品最新价的近似指标；窗口差额不是已证实的实际损失，货币单位未经核实。")

        # 仅返回不可用观察点，不把观察点扩展为整个窗口的状态或已证实因果。
        price = compute.item_price(item_id)
        if price:
            lines.append(f"商品最新价格: {price:.2f}")
        unavailable = compute.item_unavailable_periods(item_id, start, end)
        if unavailable:
            dates = ", ".join(u["date"] for u in unavailable)
            lines.append(f"商品存在不可用记录（available=0）: {dates}；仅为观察点，不能单独确认整个窗口不可售或成交下降的原因。")

        return {
            "ok": True,
            "text": "\n".join(lines),
            "rows": len(series),
            "data": {
                "series": series,
                "summary": summary,
                "price": price,
                "unavailable_dates": [u["date"] for u in unavailable],
                "coverage": coverage,
            },
        }
