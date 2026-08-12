"""FunnelTool：分析曝光→加购→成交漏斗，定位异常环节。"""
from __future__ import annotations

from app.agent.tool import Tool
from app.agent.tools._common import validate_date, validate_item_id
from app.metrics import compute


class FunnelTool(Tool):
    name = "funnel"
    description = (
        "分析商品 曝光/浏览→加购→成交 漏斗各环节的量级与转化率，"
        "用于定位异常发生在漏斗的哪个环节（浏览环节/加购环节/支付环节）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "item_id": {"type": "integer", "description": "商品 ID"},
            "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
            "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
        },
        "required": ["item_id", "start_date", "end_date"],
    }

    def run(self, item_id, start_date, end_date):
        item_id = validate_item_id(item_id)
        start = validate_date(start_date)
        end = validate_date(end_date)

        f = compute.funnel(item_id, start, end)
        lines = [f"商品 {item_id} 漏斗（{start}~{end}）:"]
        for s in f["stages"]:
            extra = []
            if "rate_from_view" in s:
                extra.append(f"占总浏览 {s['rate_from_view'] * 100:.2f}%")
            if "rate_from_addcart" in s:
                extra.append(f"占加购 {s['rate_from_addcart'] * 100:.2f}%")
            suffix = f"（{'，'.join(extra)}）" if extra else ""
            lines.append(f"  {s['label']}: {s['count']}{suffix}")
        return {"ok": True, "text": "\n".join(lines), "rows": len(f["stages"]), "data": f}
