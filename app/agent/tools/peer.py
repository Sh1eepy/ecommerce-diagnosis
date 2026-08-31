"""PeerTool：保留单窗口横向对照，不推断同行历史趋势。"""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from app.agent.tools._common import tool_parameters, parse_item_window, validate_metrics
from app.metrics import compute
from app.metrics.registry import compute_metrics


name = "peer"
description = (
    "查询商品、类目整体和排除自身的同行在当前窗口的指标及同类目 UV TOP5。"
    "仅为本窗口横向对照，没有同行历史基线；不能据此确认大盘正常或排除大盘影响。"
)
parameters = tool_parameters(metrics=True)


def query_peer(item_id, start_date, end_date, metrics=None):
    item_id, start, end = parse_item_window(item_id, start_date, end_date)
    metric_names = validate_metrics(metrics, ["uv", "cvr", "gmv", "avg_price"])
    category_id = compute.item_category_id(item_id)
    lines = [f"商品 {item_id} 同类目对比（{start}~{end}）:"]
    if category_id is None:
        lines.append("商品无类目信息，无法进行跨商品对比。")
        return {"ok": True, "text": "\n".join(lines), "rows": 0, "data": {"category_id": None}}
    # 保留原有 category 切片 - 自身口径；每份原始汇总只查一次。
    own_raw = compute.item_summary_raw(item_id, start, end)
    category_raw = compute.category_total_raw(category_id, start, end)
    own = compute_metrics(own_raw, metric_names)
    category = compute_metrics(category_raw, metric_names)
    peers = compute.peers_from_totals(category_raw, own_raw, metric_names)
    top = compute.peer_items(category_id, start, end)
    data = {"category_id": category_id, "own": own, "category_total": category,
            "category": category, "peers": peers, "top_peers": top}
    lines.extend([f"类目: {category_id}", f"商品自身: {own}", f"类目整体: {category}",
                  f"同行（排除自身）: {peers}",
                  "同类目 UV TOP5: " + ", ".join(f"{r['item_id']}(uv={r['uv']})" for r in top),
                  "证据限制：仅有本窗口横向对照，没有同行历史基线；不能据此确认大盘正常或排除大盘影响。",
                  "GMV 为最新价格近似指标，差额不等于实际损失。"])
    return {"ok": True, "text": "\n".join(lines), "rows": len(top), "data": data}


def PeerTool() -> StructuredTool:
    """返回原生 LangChain 工具；业务入口统一由注册表校验与审计。"""
    return StructuredTool(name=name, description=description, args_schema=parameters, func=query_peer)
