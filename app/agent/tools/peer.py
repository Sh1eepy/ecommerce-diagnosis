"""PeerTool：同类目跨商品对比——判断异常是"商品自身问题"还是"类目/大盘问题"。"""
from __future__ import annotations

from app.agent.tool import Tool
from app.agent.tools._common import validate_date, validate_item_id, validate_metrics
from app.metrics import compute
from app.metrics.registry import KNOWN_METRICS


class PeerTool(Tool):
    name = "peer"
    description = (
        "对比商品在同类目中的表现：返回同类目大盘整体指标、排除自身后的同行汇总指标，"
        "以及同类目 UV TOP 商品列表。用于判断异常是'商品自身问题'还是'类目/大盘问题'"
        "（如果类目整体也在跌，则异常很可能是被大盘带崩）。"
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
        metric_names = validate_metrics(metrics, ["uv", "cvr", "gmv", "avg_price"])

        cat = compute.item_category_id(item_id)
        lines = [f"商品 {item_id} 同类目对比（{start}~{end}）:"]
        if cat is None:
            lines.append("  商品无类目信息（item_category 无记录），无法进行跨商品对比。")
            return {
                "ok": True,
                "text": "\n".join(lines),
                "rows": 0,
                "data": {"category_id": None},
            }

        own = compute.item_summary(item_id, start, end, metric_names)
        cat_total = compute.category_summary(cat, start, end, metric_names)
        peers = compute.peers_stats(item_id, start, end, metric_names)
        top = compute.peer_items(cat, start, end, limit=5)

        lines.append(f"  所属类目: {cat}")
        lines.append(f"  商品自身窗口汇总: {own['current']}")
        lines.append(f"  类目大盘: {cat_total}")
        if peers:
            lines.append(f"  同行(类目-自身): {peers['peers']}")
        lines.append("  同类目 UV TOP5: " + ", ".join(
            f"{p['item_id']}(uv={p['uv']})" for p in top
        ))

        return {
            "ok": True,
            "text": "\n".join(lines),
            "rows": len(top),
            "data": {
                "category_id": cat,
                "own": own["current"],
                "category_total": cat_total,
                "category": cat_total,
                "peers": peers["peers"] if peers else None,
                "top_peers": top,
            },
        }
