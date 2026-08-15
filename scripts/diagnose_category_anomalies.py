"""为 open 的类目级异常创建诊断任务（锚点商品由 Worker 动态选取：该类目 UV 最高商品）。

类目级异常（item_id=0）设计上需要锚点商品才能喂给商品级 Agent；
本脚本把已有/新产生的类目异常批量入队，配合 Worker 消费即可产出"类目视角"报告。

用法：
  python scripts/diagnose_category_anomalies.py --limit 10      # 最近的 10 个类目异常
  python scripts/diagnose_category_anomalies.py --category 725  # 指定某个类目
  python scripts/diagnose_category_anomalies.py --all           # 全部 open 类目异常
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db, read_session  # noqa: E402
from app.models import AnomalyEvent  # noqa: E402
from app.tasks.queue import create_task  # noqa: E402


def main() -> None:
    init_db()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=10, help="最多创建任务数（默认 10）")
    ap.add_argument("--category", type=int, default=None, help="只处理指定类目")
    ap.add_argument("--all", action="store_true", help="处理全部 open 类目级异常")
    args = ap.parse_args()

    with read_session() as s:
        q = s.query(AnomalyEvent).filter(
            AnomalyEvent.status == "open",
            AnomalyEvent.item_id == 0,
            AnomalyEvent.category_id.isnot(None),
        )
        if args.category:
            q = q.filter(AnomalyEvent.category_id == args.category)
        if not args.all:
            q = q.order_by(AnomalyEvent.detected_at.desc()).limit(args.limit)
        rows = q.all()

    if not rows:
        print("没有符合条件的 open 类目级异常")
        return

    created = skipped = 0
    for anom in rows:
        task = create_task(
            "diagnose",
            payload={
                "item_id": 0,
                "category_id": anom.category_id,
                "start_date": str(anom.date_start - timedelta(days=3)),
                "end_date": str(anom.date_end),
                "anomaly": f"[类目级] {anom.description}",
            },
            anomaly_id=anom.id,
            idempotency_key=f"diag-cat-anom:{anom.id}",
        )
        if task.status == "pending" and task.attempts == 0:
            created += 1
        else:
            skipped += 1  # 已存在同 key 任务（幂等跳过）
    print(f"类目级异常 {len(rows)} 条：新建任务 {created}，幂等跳过 {skipped}")
    print("启动 Worker 消费：python scripts/run_worker.py")


if __name__ == "__main__":
    main()
