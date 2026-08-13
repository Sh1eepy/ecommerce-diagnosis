"""从真实异常事件生成评估用例（抽样，控制 LLM 调用成本）。

用法：
  python scripts/build_eval_cases.py --sample 25 --min-uv 500
输出：evaluation/evaluation_cases.json（覆盖）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import get_read_engine  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent.parent / "evaluation" / "evaluation_cases.json"

# 不同指标对应的报告关键词（中英文变体，命中任一即算覆盖该主题）
METRIC_KEYWORDS = {
    "cvr": ["转化率", "cvr", "CVR", "支付转化"],
    "gmv": ["GMV", "gmv", "销售额"],
    "uv": ["UV", "uv", "流量", "访客"],
    "addcart_rate": ["加购", "购物车"],
}


def main(sample: int = 30, min_uv: int = 500) -> None:
    sql = """
        SELECT a.item_id, a.metric, a.description, a.date_start, a.date_end
        FROM anomaly_event a
        JOIN (
            SELECT item_id, SUM(uv) AS uv FROM daily_item_stat
            WHERE dimension_type='all' AND dimension='all'
              AND stat_date BETWEEN '2015-08-28' AND '2015-09-18'
            GROUP BY item_id HAVING uv >= :min_uv
        ) t ON t.item_id = a.item_id
        WHERE a.item_id != 0 AND a.status='open'
        ORDER BY a.id
        LIMIT :sample
    """
    with get_read_engine().connect() as conn:
        rows = conn.execute(text(sql), {"min_uv": min_uv, "sample": sample}).mappings()
        cases = []
        for r in rows:
            start = r["date_start"] - timedelta(days=3)
            cases.append({
                "case_id": f"real-{r['item_id']}-{r['metric']}",
                "item_id": r["item_id"],
                "start_date": str(start),
                "end_date": str(r["date_end"]),
                "anomaly": r["description"],
                "expected_tools": ["metric", "funnel"],
                "expected_keywords": METRIC_KEYWORDS.get(r["metric"], []),
                "note": "真实异常采样",
            })
    CASES_PATH.write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"生成 {len(cases)} 个评估用例 -> {CASES_PATH}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=30, help="采样数量")
    ap.add_argument("--min-uv", type=int, default=500, help="商品窗口内最小 UV（过滤低流量噪音）")
    args = ap.parse_args()
    main(args.sample, args.min_uv)
