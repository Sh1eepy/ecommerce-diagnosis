"""运行异常检测：默认扫描数据范围内最近 14 天（前扩 7 天以支持周环比），并自动为新异常创建诊断任务。

用法：
  python scripts/run_detection.py                 # 用数据最大日期作结束日
  python scripts/run_detection.py --end 2015-09-01 --days 14
  python scripts/run_detection.py --limit 200     # 只扫前 200 个商品（调试）
  python scripts/run_detection.py --no-diagnose   # 只检测，不自动创建诊断任务
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import init_db  # noqa: E402
from app.detection.detector import data_date_range, run_detection  # noqa: E402


def main() -> None:
    init_db()
    ap = argparse.ArgumentParser()
    ap.add_argument("--end", type=str, default=None, help="结束日期 YYYY-MM-DD，默认数据最大日期")
    ap.add_argument("--days", type=int, default=14, help="扫描窗口天数")
    ap.add_argument("--limit", type=int, default=None, help="限制商品数量（调试用）")
    ap.add_argument("--no-diagnose", action="store_true", help="只检测，不自动创建诊断任务")
    args = ap.parse_args()

    _, max_date = data_date_range()
    end = date.fromisoformat(args.end) if args.end else max_date
    start = end - timedelta(days=args.days + 7)  # 前扩以支持周环比规则

    n = run_detection(start, end, limit_items=args.limit, auto_diagnose=not args.no_diagnose)
    print(f"扫描 {start} ~ {end}，生成 {n} 个新异常事件"
          + ("" if args.no_diagnose else "，并自动创建诊断任务"))


if __name__ == "__main__":
    main()
